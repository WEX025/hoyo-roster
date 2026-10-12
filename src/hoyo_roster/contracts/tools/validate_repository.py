#!/usr/bin/env python3
"""Validate the character-state repository contract without network access."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

GAMES = ("genshin-impact", "honkai-star-rail", "zenless-zone-zero")
Mutation = Callable[[Path], object]
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
DECIMAL_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
SCHEMA_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")
GENSHIN_ROSTER_CHARACTER_LOCATION_RE = re.compile(
    r"^data/genshin-impact/roster\.json\.characters\.[1-9][0-9]{0,31}$"
)
SUPPORTED_SCHEMA_SPECIFICATIONS = {
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft/2020-12/schema#",
}
EXPECTED_MANIFEST = {
    "genshin-impact": {
        "profile": "data/genshin-impact/profile.json",
        "roster": "data/genshin-impact/roster.json",
        "builds_dir": "data/genshin-impact/builds",
    },
    "honkai-star-rail": {
        "profile": "data/honkai-star-rail/profile.json",
        "roster": "data/honkai-star-rail/roster.json",
        "builds_dir": "data/honkai-star-rail/builds",
    },
    "zenless-zone-zero": {
        "profile": "data/zenless-zone-zero/profile.json",
        "roster": "data/zenless-zone-zero/roster.json",
        "builds_dir": "data/zenless-zone-zero/builds",
        "bangboos": "data/zenless-zone-zero/bangboos.json",
    },
}
SCHEMA_PATHS = {
    "schemas/common/definitions.schema.json",
    "schemas/repository-manifest.schema.json",
    "schemas/genshin-impact/profile.schema.json",
    "schemas/genshin-impact/roster.schema.json",
    "schemas/genshin-impact/build.schema.json",
    "schemas/honkai-star-rail/profile.schema.json",
    "schemas/honkai-star-rail/roster.schema.json",
    "schemas/honkai-star-rail/build.schema.json",
    "schemas/zenless-zone-zero/profile.schema.json",
    "schemas/zenless-zone-zero/roster.schema.json",
    "schemas/zenless-zone-zero/build.schema.json",
    "schemas/zenless-zone-zero/bangboos.schema.json",
}
DATA_PATHS = {
    "data/manifest.json",
    *(entry["profile"] for entry in EXPECTED_MANIFEST.values()),
    *(entry["roster"] for entry in EXPECTED_MANIFEST.values()),
    EXPECTED_MANIFEST["zenless-zone-zero"]["bangboos"],
}
EQUIPMENT_KEYS = {
    "genshin-impact": ("flower", "plume", "sands", "goblet", "circlet"),
    "honkai-star-rail": (
        "head",
        "hands",
        "body",
        "feet",
        "planar_sphere",
        "link_rope",
    ),
    "zenless-zone-zero": (
        "slot_1",
        "slot_2",
        "slot_3",
        "slot_4",
        "slot_5",
        "slot_6",
    ),
}
EQUIPMENT_CONTAINER = {
    "genshin-impact": "artifacts",
    "honkai-star-rail": "relics",
    "zenless-zone-zero": "drive_discs",
}
PANEL_KEYS = {
    "genshin-impact": (
        "hp",
        "attack",
        "defense",
        "elemental_mastery",
        "crit_rate",
        "crit_damage",
        "energy_recharge",
        "healing_bonus",
        "physical_damage_bonus",
        "pyro_damage_bonus",
        "hydro_damage_bonus",
        "electro_damage_bonus",
        "cryo_damage_bonus",
        "anemo_damage_bonus",
        "geo_damage_bonus",
        "dendro_damage_bonus",
    ),
    "honkai-star-rail": (
        "hp",
        "attack",
        "defense",
        "speed",
        "crit_rate",
        "crit_damage",
        "break_effect",
        "effect_hit_rate",
        "effect_res",
        "energy_regeneration_rate",
        "outgoing_healing_boost",
        "physical_damage_bonus",
        "fire_damage_bonus",
        "ice_damage_bonus",
        "lightning_damage_bonus",
        "wind_damage_bonus",
        "quantum_damage_bonus",
        "imaginary_damage_bonus",
    ),
    "zenless-zone-zero": (
        "hp",
        "attack",
        "defense",
        "impact",
        "crit_rate",
        "crit_damage",
        "anomaly_proficiency",
        "anomaly_mastery",
        "penetration",
        "penetration_ratio",
        "energy_regeneration",
        "sheer_force",
        "flash_energy_accumulation",
        "physical_damage_bonus",
        "fire_damage_bonus",
        "ice_damage_bonus",
        "electric_damage_bonus",
        "ether_damage_bonus",
        "wind_damage_bonus",
    ),
}
MAIN_STAT_KEYS = {
    "genshin-impact": {
        "flower": "hp",
        "plume": "attack",
        "sands": "energy_recharge",
        "goblet": "pyro_damage_bonus",
        "circlet": "crit_rate",
    },
    "honkai-star-rail": {
        "head": "hp",
        "hands": "attack",
        "body": "crit_rate",
        "feet": "speed",
        "planar_sphere": "wind_damage_bonus",
        "link_rope": "energy_regeneration_rate",
    },
    "zenless-zone-zero": {
        "slot_1": "hp",
        "slot_2": "attack",
        "slot_3": "defense",
        "slot_4": "crit_rate",
        "slot_5": "wind_damage_bonus",
        "slot_6": "impact",
    },
}
FORBIDDEN_FIELDS = {
    "instance_id",
    "weapon_instance_id",
    "equipment_instance_ids",
    "equipped_by",
    "owner_character",
    "locked",
    "roll_count",
    "friendship_level",
    "trust_level",
    "build_state",
    "coverage",
    "observed_at",
    "equipment_id",
    "technique_level",
    "character_id",
}
AUTH_FIELD_TOKENS = (
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "passwords",
    "secret",
    "secrets",
    "session",
    "sessions",
    "token",
    "tokens",
)
AUTH_FILE_PATTERNS = (
    "cookies",
    "login data",
    "network persistent state",
    "storage_state*.json",
    "auth_state*.json",
    "cookies*.json",
    "cookie*.json",
    "credentials*.json",
    "secrets*.json",
    "*.har",
    "trace.zip",
    "*.webm",
)
AUTH_DIRECTORY_NAMES = {
    "browser-profile",
    "browser_profile",
    "profile-data",
    "profile_data",
    "local storage",
    "session storage",
    "screenshots",
    "screen-recordings",
    "recordings",
    "videos",
    "traces",
}
IGNORED_SCAN_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
_SCHEMA_VALIDATOR_CACHE: dict[
    str,
    tuple[tuple[str, ...], dict[str, Any], dict[str, Any]],
] = {}


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _require_regular_file(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"not a regular file: {path.name}")


def _require_real_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"not a real directory: {path.name}")


def _exact_json_number(token: str) -> Decimal | int:
    number = Decimal(token)
    # JSON Schema integer semantics concern the value, not the decimal token.
    # Convert only an exactly integral value; never round or cast via float.
    if number.is_finite() and number == number.to_integral_value():
        return int(number)
    return number


def load_json(path: Path, *, exact_numbers: bool = False) -> Any:
    _require_regular_file(path)
    text = path.read_bytes().decode("utf-8")
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicates,
        parse_constant=_reject_constant,
        parse_float=_exact_json_number if exact_numbers else float,
    )


def _exact_json_text(value: Any, *, level: int = 0) -> str:
    """Match the ZZZ writer without rounding JSON numbers through float."""
    if isinstance(value, float):
        value = Decimal(str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number is forbidden")
        # format(..., 'f') does not apply the active Decimal context precision.
        token = format(value, "f")
        if "." in token:
            token = token.rstrip("0").rstrip(".")
        return "0" if value == 0 else token
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = [
            " " * (2 * (level + 1))
            + json.dumps(key, ensure_ascii=False)
            + ": "
            + _exact_json_text(child, level=level + 1)
            for key, child in value.items()
        ]
        return "{\n" + ",\n".join(rows) + "\n" + " " * (2 * level) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        rows = [
            " " * (2 * (level + 1)) + _exact_json_text(child, level=level + 1) for child in value
        ]
        return "[\n" + ",\n".join(rows) + "\n" + " " * (2 * level) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def write_json(path: Path, value: Any) -> None:
    text = (
        _exact_json_text(value)
        if "zenless-zone-zero" in path.parts
        else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    )
    data = (text + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_real_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _schema_path_for_data(path: str) -> str | None:
    if path == "data/manifest.json":
        return "schemas/repository-manifest.schema.json"
    if path == "data/zenless-zone-zero/bangboos.json":
        return "schemas/zenless-zone-zero/bangboos.schema.json"
    for game in GAMES:
        prefix = f"data/{game}/"
        if not path.startswith(prefix):
            continue
        suffix = path[len(prefix) :]
        if suffix == "profile.json":
            return f"schemas/{game}/profile.schema.json"
        if suffix == "roster.json":
            return f"schemas/{game}/roster.schema.json"
        if suffix.startswith("builds/") and suffix.endswith(".json"):
            return f"schemas/{game}/build.schema.json"
    return None


def _scope_games(scope_game: str | None) -> tuple[str, ...]:
    return GAMES if scope_game is None else (scope_game,)


def _scope_schema_paths(scope_game: str | None) -> set[str]:
    if scope_game is None:
        return set(SCHEMA_PATHS)
    return {
        "schemas/common/definitions.schema.json",
        "schemas/repository-manifest.schema.json",
        f"schemas/{scope_game}/profile.schema.json",
        f"schemas/{scope_game}/roster.schema.json",
        f"schemas/{scope_game}/build.schema.json",
        *(
            {"schemas/zenless-zone-zero/bangboos.schema.json"}
            if scope_game == "zenless-zone-zero"
            else set()
        ),
    }


def _scope_data_paths(scope_game: str | None) -> set[str]:
    if scope_game is None:
        return set(DATA_PATHS)
    entry = EXPECTED_MANIFEST[scope_game]
    return {
        "data/manifest.json",
        entry["profile"],
        entry["roster"],
        *({entry["bangboos"]} if "bangboos" in entry else set()),
    }


def _allowed_directories(scope_game: str | None) -> set[str]:
    games = _scope_games(scope_game)
    directories = {"data", "schemas", "schemas/common"}
    for game in games:
        directories.update(
            {
                f"data/{game}",
                EXPECTED_MANIFEST[game]["builds_dir"],
                f"schemas/{game}",
            }
        )
    return directories


def _scan_canonical_files(
    root: Path,
    *,
    scope_game: str | None,
) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    errors: list[str] = []
    allowed_directories = _allowed_directories(scope_game)
    for required_dir in (root / "data", root / "schemas", root / "tools"):
        relative = rel(required_dir, root)
        try:
            _require_real_directory(required_dir)
        except (OSError, FileNotFoundError) as error:
            errors.append(f"{relative}: required real directory missing or unsafe: {error}")

    validator = root / "tools/validate_repository.py"
    try:
        _require_regular_file(validator)
    except (OSError, FileNotFoundError) as error:
        errors.append(
            f"tools/validate_repository.py: required regular Validator missing or unsafe: {error}"
        )

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            errors.append(f"{rel(directory, root)}: directory inspection failed: {error}")
            return
        for entry in entries:
            path = Path(entry.path)
            relative = rel(path, root)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                errors.append(f"{relative}: lstat failed: {error}")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                errors.append(f"{relative}: symbolic links are forbidden")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in allowed_directories:
                    errors.append(f"{relative}: unexpected canonical directory")
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                errors.append(f"{relative}: non-regular files are forbidden")
                continue
            files.append(path)

    for canonical_root in (root / "data", root / "schemas"):
        try:
            _require_real_directory(canonical_root)
        except (OSError, FileNotFoundError):
            continue
        visit(canonical_root)
    return files, errors


def _load_documents(
    root: Path,
    *,
    scope_game: str | None,
) -> tuple[dict[str, Any], list[Path], list[str]]:
    documents: dict[str, Any] = {}
    canonical_files, errors = _scan_canonical_files(root, scope_game=scope_game)
    for path in canonical_files:
        if path.suffix != ".json":
            continue
        relative = rel(path, root)
        try:
            documents[relative] = load_json(
                path,
                exact_numbers=relative.startswith("data/zenless-zone-zero/"),
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            DuplicateKeyError,
        ) as exc:
            errors.append(f"{relative}: {exc}")
    return documents, canonical_files, errors


def _schema_errors(
    documents: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    try:
        from jsonschema import (  # type: ignore[import-untyped]
            Draft202012Validator,
            FormatChecker,
        )
        from referencing import Registry, Resource
    except ImportError:
        return ["jsonschema is required for Schema and fixture validation"], {}

    schema_docs = {path: value for path, value in documents.items() if path.startswith("schemas/")}
    digest = hashlib.sha256()
    try:
        for path, schema in sorted(schema_docs.items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(
                json.dumps(
                    schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\0")
    except (TypeError, ValueError) as exc:
        return [f"schemas: Schema digest construction failed: {type(exc).__name__}: {exc}"], {}
    cache_key = digest.hexdigest()
    cached = _SCHEMA_VALIDATOR_CACHE.get(cache_key)
    if cached is None:
        setup_errors: list[str] = []
        resource_entries: list[tuple[str, Any]] = []
        schema_id_owners: dict[str, str] = {}
        for path, schema in sorted(schema_docs.items()):
            if not isinstance(schema, dict):
                setup_errors.append(f"{path}: Schema must be an object")
                continue
            raw_schema_id = schema.get("$id")
            schema_id = (
                raw_schema_id
                if isinstance(raw_schema_id, str)
                and raw_schema_id
                and SCHEMA_ID_RE.fullmatch(raw_schema_id)
                else None
            )
            if schema_id is None:
                if raw_schema_id is None:
                    setup_errors.append(f"{path}: Schema $id is required")
                else:
                    setup_errors.append(
                        f"{path}: Schema $id must be a non-empty absolute URI string"
                    )
            elif schema_id in schema_id_owners:
                setup_errors.append(
                    f"{path}: duplicate Schema $id {schema_id!r}; "
                    f"first declared by {schema_id_owners[schema_id]}"
                )
            else:
                schema_id_owners[schema_id] = path

            specification = schema.get("$schema")
            specification_supported = specification in SUPPORTED_SCHEMA_SPECIFICATIONS
            if specification is None:
                setup_errors.append(f"{path}: Schema $schema is required")
            elif not isinstance(specification, str) or not specification_supported:
                setup_errors.append(f"{path}: unsupported $schema {specification!r}")

            resource: Any | None = None
            try:
                resource = Resource.from_contents(schema)
            except Exception as exc:
                setup_errors.append(
                    f"{path}: Schema resource construction failed: {type(exc).__name__}: {exc}"
                )
            if (
                resource is not None
                and schema_id is not None
                and schema_id_owners.get(schema_id) == path
                and specification_supported
            ):
                resource_entries.append((schema_id, resource))

            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                setup_errors.append(f"{path}: invalid Schema: {type(exc).__name__}: {exc}")

        registry: Any | None = None
        if not setup_errors:
            try:
                registry = Registry().with_resources(resource_entries)
            except Exception as exc:
                setup_errors.append(
                    f"schemas: Schema Registry construction failed: {type(exc).__name__}: {exc}"
                )

        validators: dict[str, Any] = {}
        complete_build_validators: dict[str, Any] = {}
        if not setup_errors and registry is not None:
            for path, schema in sorted(schema_docs.items()):
                try:
                    validators[path] = Draft202012Validator(
                        schema,
                        registry=registry,
                        format_checker=FormatChecker(),
                    )
                except Exception as exc:
                    setup_errors.append(
                        f"{path}: Schema Validator construction failed: {type(exc).__name__}: {exc}"
                    )
            for game in GAMES:
                path = f"schemas/{game}/build.schema.json"
                schema = schema_docs.get(path)
                if schema is None:
                    continue
                definitions = schema.get("$defs") if isinstance(schema, dict) else None
                if not isinstance(definitions, dict) or not isinstance(
                    definitions.get("complete_build"),
                    dict,
                ):
                    setup_errors.append(f"{path}: $defs.complete_build is required")
                    continue
                try:
                    complete_build_validators[game] = Draft202012Validator(
                        {"$ref": (f"{schema['$id']}#/$defs/complete_build")},
                        registry=registry,
                        format_checker=FormatChecker(),
                    )
                except Exception as exc:
                    setup_errors.append(
                        f"{path}: complete Build Validator construction failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        _SCHEMA_VALIDATOR_CACHE[cache_key] = (
            tuple(setup_errors),
            validators,
            complete_build_validators,
        )
    else:
        cached_errors, validators, complete_build_validators = cached
        setup_errors = list(cached_errors)
    if setup_errors:
        return setup_errors, {}

    errors: list[str] = []
    for path, instance in sorted(documents.items()):
        if not path.startswith("data/"):
            continue
        schema_path = _schema_path_for_data(path)
        if schema_path is None:
            errors.append(f"{path}: no Schema mapping")
            continue
        validator = validators.get(schema_path)
        if validator is None:
            errors.append(f"{path}: missing mapped Schema {schema_path}")
            continue
        try:
            issues = sorted(
                validator.iter_errors(instance),
                key=lambda item: list(item.path),
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            lowered = detail.lower()
            label = (
                "Schema reference resolution failed"
                if "referenc" in lowered or "resolv" in lowered
                else "Schema validation failed"
            )
            errors.append(f"{path}: {label}: {detail}")
            continue
        for issue in issues:
            location = ".".join(str(part) for part in issue.path) or "<root>"
            errors.append(f"{path}:{location}: {issue.message}")
    return errors, complete_build_validators


def _valid_build_id(game: str, character_id: str) -> bool:
    pattern = DECIMAL_ID_RE if game == "zenless-zone-zero" else ID_RE
    return pattern.fullmatch(character_id) is not None


def _validate_layout(
    root: Path,
    documents: dict[str, Any],
    canonical_files: list[Path],
    *,
    scope_game: str | None,
) -> list[str]:
    errors: list[str] = []
    expected_schemas = _scope_schema_paths(scope_game)
    expected_data = _scope_data_paths(scope_game)
    games = _scope_games(scope_game)
    actual_schemas = {path for path in documents if path.startswith("schemas/")}
    for path in sorted(expected_schemas - actual_schemas):
        errors.append(f"{path}: required Schema missing")
    for path in sorted(actual_schemas - expected_schemas):
        errors.append(f"{path}: forbidden or obsolete Schema")

    build_dirs = {EXPECTED_MANIFEST[game]["builds_dir"]: game for game in games}
    for relative_path in sorted(documents):
        if relative_path in expected_schemas or relative_path in expected_data:
            continue
        candidate = Path(relative_path)
        game = build_dirs.get(candidate.parent.as_posix())
        if (
            game is not None
            and candidate.suffix == ".json"
            and _valid_build_id(game, candidate.stem)
        ):
            continue
        errors.append(f"{relative_path}: unexpected canonical JSON path")

    build_markers = {f"{EXPECTED_MANIFEST[game]['builds_dir']}/.gitkeep" for game in games}
    for canonical_path in canonical_files:
        relative = rel(canonical_path, root)
        candidate = Path(relative)
        game = build_dirs.get(candidate.parent.as_posix())
        future_build = bool(
            game is not None
            and candidate.suffix == ".json"
            and _valid_build_id(game, candidate.stem)
        )
        if relative.startswith("data/"):
            if relative not in expected_data and relative not in build_markers and not future_build:
                errors.append(f"{relative}: unexpected canonical data path")
        elif relative.startswith("schemas/") and relative not in expected_schemas:
            errors.append(f"{relative}: unexpected canonical Schema path")

    file_relatives = {rel(canonical_path, root) for canonical_path in canonical_files}
    for game in games:
        build_dir = root / EXPECTED_MANIFEST[game]["builds_dir"]
        try:
            _require_real_directory(build_dir)
        except (OSError, FileNotFoundError):
            errors.append(f"{rel(build_dir, root)}: required real directory missing")
        marker = f"{rel(build_dir, root)}/.gitkeep"
        if marker not in file_relatives:
            errors.append(f"{rel(build_dir, root)}/.gitkeep: required marker missing")

    for canonical_path in canonical_files:
        relative = rel(canonical_path, root)
        if relative.startswith("data/") and canonical_path.name == "resources.json":
            errors.append(f"{relative}: forbidden resource data")
        if (
            relative.startswith("schemas/")
            and "resources" in canonical_path.name
            and canonical_path.name.endswith(".schema.json")
        ):
            errors.append(f"{relative}: forbidden resource Schema")
    return errors


def _auth_field_errors(value: Any, location: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_tokens = {token for token in re.split(r"[^a-z0-9]+", key.lower()) if token}
            if key_tokens.intersection(AUTH_FIELD_TOKENS):
                errors.append(f"{location}: forbidden authentication field {key!r}")
            _auth_field_errors(child, f"{location}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _auth_field_errors(child, f"{location}[{index}]", errors)


def _validate_auth_artifacts(root: Path) -> list[str]:
    errors: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name.lower()
            path = Path(entry.path)
            relative = rel(path, root)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if name in AUTH_DIRECTORY_NAMES and (
                stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            ):
                errors.append(f"{relative}: forbidden authentication artifact directory")
                continue
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                if name in IGNORED_SCAN_DIRECTORY_NAMES:
                    continue
                visit(path)
                continue

            forbidden_name = (
                name == ".env"
                or name.startswith(".env.")
                or any(fnmatch.fnmatch(name, pattern) for pattern in AUTH_FILE_PATTERNS)
            )
            if forbidden_name:
                errors.append(f"{relative}: forbidden authentication artifact")
            if not stat.S_ISREG(metadata.st_mode) or path.suffix.lower() != ".json":
                continue
            try:
                value = load_json(path)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue
            _auth_field_errors(value, relative, errors)

    visit(root)
    return errors


def _validate_manifest(
    root: Path,
    documents: dict[str, Any],
    *,
    scope_game: str | None,
) -> list[str]:
    manifest = documents.get("data/manifest.json")
    if not isinstance(manifest, dict):
        return ["data/manifest.json: missing or invalid"]
    errors: list[str] = []
    if manifest.get("games") != EXPECTED_MANIFEST:
        errors.append("data/manifest.json: fixed game bindings differ from contract")
    for game in _scope_games(scope_game):
        entry = EXPECTED_MANIFEST[game]
        for key, target in entry.items():
            try:
                if key == "builds_dir":
                    _require_real_directory(root / target)
                else:
                    _require_regular_file(root / target)
            except (OSError, FileNotFoundError):
                errors.append(f"data/manifest.json:{key}: target {target} does not exist")
    return errors


def _validate_game_fields(documents: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for path, value in documents.items():
        if not path.startswith("data/") or path == "data/manifest.json":
            continue
        parts = Path(path).parts
        if len(parts) >= 3 and isinstance(value, dict) and value.get("game") != parts[1]:
            errors.append(f"{path}: game does not match directory")
    return errors


def _walk_fields(value: Any, location: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            allowed_genshin_friendship = (
                key == "friendship_level"
                and GENSHIN_ROSTER_CHARACTER_LOCATION_RE.fullmatch(location) is not None
            )
            if key in FORBIDDEN_FIELDS and not allowed_genshin_friendship or "instance_id" in key:
                errors.append(f"{location}: forbidden legacy field {key!r}")
            if key == "nodes" and location.endswith("potential_vision"):
                errors.append(f"{location}: potential_vision.nodes is forbidden")
            _walk_fields(child, f"{location}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_fields(child, f"{location}[{index}]", errors)
    elif isinstance(value, str) and value == "":
        errors.append(f"{location}: empty string is forbidden")


def _validate_common_definition_references(documents: dict[str, Any]) -> list[str]:
    path = "schemas/common/definitions.schema.json"
    definitions = documents.get(path, {}).get("$defs", {})
    if not isinstance(definitions, dict):
        return [f"{path}: $defs missing"]
    refs: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                refs.append(ref)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for schema_path, schema in documents.items():
        if schema_path.startswith("schemas/"):
            collect(schema)
    prefix = "urn:hoyo-game-data:schema:common:definitions:v1#/$defs/"
    return [
        f"{path}: dead definition {name!r}" for name in definitions if f"{prefix}{name}" not in refs
    ]


def _validate_canonical_format(root: Path, documents: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for path, value in documents.items():
        exact_zzz = path.startswith("data/zenless-zone-zero/")
        expected = (
            (_exact_json_text(value) + "\n")
            if exact_zzz
            else (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
        )
        try:
            _require_regular_file(root / path)
            actual = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if actual != expected:
            errors.append(f"{path}: JSON is not canonical two-space format")
    return errors


def _validate_sorted_map(mapping: Any, location: str, errors: list[str]) -> None:
    if isinstance(mapping, dict) and list(mapping) != sorted(mapping):
        errors.append(f"{location}: map keys must be sorted lexicographically")


def _validate_cross_file(
    root: Path,
    documents: dict[str, Any],
    *,
    scope_game: str | None,
    complete_build_validators: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for game in _scope_games(scope_game):
        entry = EXPECTED_MANIFEST[game]
        profile = documents.get(entry["profile"], {})
        roster = documents.get(entry["roster"], {})
        characters = roster.get("characters", {}) if isinstance(roster, dict) else {}
        if not isinstance(characters, dict):
            errors.append(f"{entry['roster']}: characters must be an object")
            continue
        _validate_sorted_map(characters, f"{entry['roster']}.characters", errors)

        build_path_keys = sorted(
            path
            for path in documents
            if Path(path).parent.as_posix() == entry["builds_dir"] and Path(path).suffix == ".json"
        )
        build_ids = {Path(path).stem for path in build_path_keys}
        roster_ids = set(characters)
        for character_id in sorted(roster_ids):
            if not _valid_build_id(game, character_id):
                errors.append(f"{entry['roster']}: invalid character ID {character_id!r}")
        for character_id in sorted(roster_ids - build_ids):
            errors.append(f"{entry['roster']}:{character_id}: missing Build")
        for character_id in sorted(build_ids - roster_ids):
            errors.append(f"{entry['builds_dir']}/{character_id}.json: orphan Build")

        state = profile.get("sync", {}).get("state") if isinstance(profile, dict) else None
        account = profile.get("account", {}) if isinstance(profile, dict) else {}
        complete_validator = complete_build_validators.get(game)
        complete_issues: dict[str, list[Any]] = {}
        if complete_validator is not None:
            for path_key in build_path_keys:
                build = documents.get(path_key)
                if not isinstance(build, dict):
                    continue
                try:
                    complete_issues[path_key] = sorted(
                        complete_validator.iter_errors(build),
                        key=lambda item: list(item.path),
                    )
                except Exception as exc:
                    errors.append(
                        f"{path_key}: complete Build validation failed: {type(exc).__name__}: {exc}"
                    )
                    complete_issues[path_key] = [exc]

        if state == "not_synced":
            if not isinstance(account, dict) or any(
                value is not None for value in account.values()
            ):
                errors.append(f"{entry['profile']}: not_synced account must be all null")
            if characters or build_ids:
                errors.append(f"{game}: not_synced snapshot must have empty roster and builds")
            if game == "zenless-zone-zero":
                bangboo_document = documents.get(entry["bangboos"])
                bangboos = (
                    bangboo_document.get("items", {})
                    if isinstance(bangboo_document, dict)
                    else None
                )
                if bangboos != {}:
                    errors.append(f"{entry['bangboos']}: not_synced items must be empty")
        elif state in {"partial", "complete"}:
            if not isinstance(account, dict) or any(value is None for value in account.values()):
                errors.append(f"{entry['profile']}: {state} account fields must be non-null")
            hsr_profile_only_partial = (
                game == "honkai-star-rail"
                and state == "partial"
                and not characters
                and not build_ids
            )
            if not characters and not hsr_profile_only_partial:
                errors.append(f"{entry['roster']}: {state} snapshot roster must be non-empty")
            if game == "honkai-star-rail" and state == "complete":
                for character_id, character in sorted(characters.items()):
                    if not isinstance(character, dict):
                        continue
                    for field in ("promotion", "eidolon"):
                        if field not in character:
                            errors.append(
                                f"{entry['roster']}.characters.{character_id}: "
                                f"complete roster requires {field}"
                            )
            if complete_validator is not None:
                if state == "partial":
                    if (
                        build_ids == roster_ids
                        and build_path_keys
                        and all(not complete_issues.get(path_key) for path_key in build_path_keys)
                    ):
                        errors.append(
                            f"{entry['profile']}: partial snapshot has only complete "
                            "Builds; sync.state must be complete"
                        )
                else:
                    for path_key in build_path_keys:
                        for issue in complete_issues.get(path_key, []):
                            if not hasattr(issue, "path"):
                                continue
                            location = ".".join(str(part) for part in issue.path) or "<root>"
                            errors.append(f"{path_key}:{location}: complete_build: {issue.message}")
        else:
            errors.append(
                f"{entry['profile']}: sync.state must be not_synced, partial, or complete"
            )

        for path_key in build_path_keys:
            build = documents.get(path_key)
            if not isinstance(build, dict):
                continue
            container = build.get(EQUIPMENT_CONTAINER[game], {})
            if isinstance(container, dict):
                for slot in EQUIPMENT_KEYS[game]:
                    item = container.get(slot)
                    if not isinstance(item, dict):
                        continue
                    substats = item.get("substats")
                    if not isinstance(substats, list):
                        continue
                    keys: list[str] = []
                    for substat in substats:
                        if not isinstance(substat, dict):
                            continue
                        key = substat.get("key")
                        if isinstance(key, str):
                            keys.append(key)
                    if len(keys) != len(set(keys)):
                        errors.append(f"{path_key}.{slot}: duplicate substat key")
                    if keys != sorted(keys):
                        errors.append(f"{path_key}.{slot}: substats must be sorted by key")
            if game == "honkai-star-rail":
                progression = build.get("progression", {})
                if isinstance(progression, dict):
                    _validate_sorted_map(
                        progression.get("trace_nodes"),
                        f"{path_key}.progression.trace_nodes",
                        errors,
                    )
                    memosprite = progression.get("memosprite", {})
                    if isinstance(memosprite, dict):
                        _validate_sorted_map(
                            memosprite.get("skills"),
                            f"{path_key}.progression.memosprite.skills",
                            errors,
                        )
                        _validate_sorted_map(
                            memosprite.get("talents"),
                            f"{path_key}.progression.memosprite.talents",
                            errors,
                        )
            if game == "zenless-zone-zero":
                potential = build.get("progression", {}).get("potential_vision")
                if isinstance(potential, dict):
                    level = potential.get("level")
                    max_level = potential.get("max_level")
                    if isinstance(level, int) and isinstance(max_level, int) and level > max_level:
                        errors.append(
                            f"{path_key}.progression.potential_vision: "
                            "level must not exceed max_level"
                        )
    if scope_game in (None, "zenless-zone-zero"):
        bangboo_document = documents.get("data/zenless-zone-zero/bangboos.json")
        bangboos = bangboo_document.get("items") if isinstance(bangboo_document, dict) else None
        _validate_sorted_map(
            bangboos,
            "data/zenless-zone-zero/bangboos.json.items",
            errors,
        )
    return errors


def _validate_empty_baseline(
    root: Path,
    documents: dict[str, Any],
    canonical_files: list[Path],
) -> list[str]:
    errors: list[str] = []
    canonical_file_paths = {rel(path, root) for path in canonical_files}
    for game in GAMES:
        entry = EXPECTED_MANIFEST[game]
        profile = documents.get(entry["profile"])
        if not isinstance(profile, dict):
            errors.append(f"{entry['profile']}: baseline profile must be an object")
            profile = {}
        if profile.get("sync") != {
            "state": "not_synced",
            "source": None,
            "synced_at": None,
        }:
            errors.append(f"{entry['profile']}: baseline sync must be not_synced")
        account = profile.get("account", {})
        if not isinstance(account, dict) or any(value is not None for value in account.values()):
            errors.append(f"{entry['profile']}: baseline account must be all null")
        roster = documents.get(entry["roster"])
        if not isinstance(roster, dict):
            errors.append(f"{entry['roster']}: baseline roster must be an object")
            roster = {}
        if roster.get("characters") != {}:
            errors.append(f"{entry['roster']}: baseline roster must be empty")
        build_dir = entry["builds_dir"]
        entries = {
            Path(path).name
            for path in canonical_file_paths
            if Path(path).parent.as_posix() == build_dir
        }
        if entries != {".gitkeep"}:
            errors.append(f"{entry['builds_dir']}: baseline must contain only .gitkeep")
    bangboos = documents.get("data/zenless-zone-zero/bangboos.json")
    if not isinstance(bangboos, dict):
        errors.append("data/zenless-zone-zero/bangboos.json: baseline Bangboos must be an object")
        bangboos = {}
    if bangboos.get("items") != {}:
        errors.append("data/zenless-zone-zero/bangboos.json: baseline items must be empty")
    json_count = len(documents)
    if json_count != 20:
        errors.append(f"empty baseline JSON_FILE_COUNT must be 20, got {json_count}")
    if len([path for path in documents if path.startswith("schemas/")]) != 12:
        errors.append("empty baseline SCHEMA_FILE_COUNT must be 12")
    return errors


def _validate_git_diff(root: Path) -> list[str]:
    if not os.path.lexists(root / ".git"):
        return []
    errors: list[str] = []
    for staged, command in (
        (False, ["git", "-C", str(root), "diff", "--check"]),
        (True, ["git", "-C", str(root), "diff", "--cached", "--check"]),
    ):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()
            label = "staged" if staged else "unstaged"
            errors.append(f"git diff --check failed for {label} changes: {detail}")
    return errors


def validate_repository(
    root: Path,
    *,
    enforce_empty: bool,
    scope_game: str | None = None,
) -> list[str]:
    root = root.resolve()
    documents, canonical_files, errors = _load_documents(
        root,
        scope_game=scope_game,
    )
    errors.extend(
        _validate_layout(
            root,
            documents,
            canonical_files,
            scope_game=scope_game,
        )
    )
    schema_errors, complete_build_validators = _schema_errors(documents)
    errors.extend(schema_errors)
    errors.extend(_validate_auth_artifacts(root))
    errors.extend(_validate_manifest(root, documents, scope_game=scope_game))
    errors.extend(_validate_game_fields(documents))
    if scope_game is None:
        errors.extend(_validate_common_definition_references(documents))
    errors.extend(_validate_canonical_format(root, documents))
    for path, value in documents.items():
        if path.startswith("data/"):
            _walk_fields(value, path, errors)
    errors.extend(
        _validate_cross_file(
            root,
            documents,
            scope_game=scope_game,
            complete_build_validators=complete_build_validators,
        )
    )
    if enforce_empty:
        if scope_game is not None:
            errors.append("--require-empty-baseline is incompatible with --scope-game")
        errors.extend(_validate_empty_baseline(root, documents, canonical_files))
    errors.extend(_validate_git_diff(root))
    return errors


def _self_test_helpers() -> Any:
    # Load only for --self-test or explicit fixture construction. Normal data
    # validation remains standalone and never imports test fixtures.
    path = Path(__file__).absolute().with_name("repository_validator_selftest.py")
    _require_real_directory(path.parent)
    _require_regular_file(path)
    spec = importlib.util.spec_from_file_location("hoyo_repository_selftest", path)
    if spec is None or spec.loader is None:
        raise OSError("repository self-test helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = SimpleNamespace(**{name: globals()[name] for name in module.API_NAMES})
    return module.RepositorySelfTest(api)


def _panel(game: str) -> dict[str, float]:
    return _self_test_helpers()._panel(game)


def _equipment(game: str, slot: str) -> dict[str, Any]:
    return _self_test_helpers()._equipment(game, slot)


def _equipment_set(game: str) -> dict[str, dict[str, Any]]:
    return _self_test_helpers()._equipment_set(game)


def _set_complete(profile: dict[str, Any], suffix: str) -> None:
    return _self_test_helpers()._set_complete(profile, suffix)


def _require_self_test_source_path(source_root: Path, relative: Path, *, directory: bool) -> None:
    return _self_test_helpers()._require_self_test_source_path(
        source_root, relative, directory=directory
    )


def _validate_self_test_source(source_root: Path) -> None:
    return _self_test_helpers()._validate_self_test_source(source_root)


def _create_empty_fixture(root: Path, source_root: Path) -> None:
    return _self_test_helpers()._create_empty_fixture(root, source_root)


def _populate_positive(root: Path) -> None:
    return _self_test_helpers()._populate_positive(root)


def _mutate_json(root: Path, path: str, mutator: Callable[[dict[str, Any]], None]) -> None:
    return _self_test_helpers()._mutate_json(root, path, mutator)


def _run_self_test(source_root: Path) -> int:
    return _self_test_helpers()._run_self_test(source_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root", type=Path)
    group.add_argument("--self-test", action="store_true")
    parser.add_argument("--require-empty-baseline", action="store_true")
    parser.add_argument("--scope-game", choices=GAMES)
    args = parser.parse_args()
    source_root = Path(__file__).absolute().parents[1]
    if args.require_empty_baseline and args.root is None:
        parser.error("--require-empty-baseline requires --root")
    if args.scope_game is not None and args.root is None:
        parser.error("--scope-game requires --root")
    if args.self_test:
        return _run_self_test(source_root)
    errors = validate_repository(
        args.root,
        enforce_empty=args.require_empty_baseline,
        scope_game=args.scope_game,
    )
    if errors:
        print("REPOSITORY_VALIDATION=FAIL")
        for error in errors:
            print(f"ERROR={error}")
        return 1
    root = args.root.resolve()
    documents, _, count_errors = _load_documents(
        root,
        scope_game=args.scope_game,
    )
    if count_errors:
        print("REPOSITORY_VALIDATION=FAIL")
        return 1
    json_count = len(documents)
    schema_count = len([path for path in documents if path.startswith("schemas/")])
    build_count = sum(
        len(
            [
                path
                for path in documents
                if Path(path).parent.as_posix() == EXPECTED_MANIFEST[game]["builds_dir"]
            ]
        )
        for game in _scope_games(args.scope_game)
    )
    print("REPOSITORY_VALIDATION=PASS")
    print(f"EMPTY_BASELINE_REQUIRED={'YES' if args.require_empty_baseline else 'NO'}")
    print(f"VALIDATION_SCOPE={args.scope_game or 'full'}")
    print("JSON_PARSE_VALIDATION=PASS")
    print("SCHEMA_SELF_VALIDATION=PASS")
    print("CANONICAL_PATH_CLOSURE=PASS")
    print("ROSTER_BUILD_BIJECTION=PASS")
    print("FORBIDDEN_LEGACY_PATH_VALIDATION=PASS")
    print("SECRET_RESIDUE_SCAN=PASS")
    print("GIT_DIFF_CHECK=PASS")
    print(f"JSON_FILE_COUNT={json_count}")
    print(f"SCHEMA_FILE_COUNT={schema_count}")
    print(f"FORMAL_BUILD_JSON_COUNT={build_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
