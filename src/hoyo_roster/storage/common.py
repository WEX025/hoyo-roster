"""Filesystem and metadata primitives for repository mutation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

SHA256_PATTERN = r"^[0-9a-f]{64}$"
PRIVATE_MODE = 0o700


class MetadataError(ValueError):
    """Mutation metadata or an owned filesystem path was invalid."""


def raise_as(
    error_type: type[Exception],
    message: str,
    cause: BaseException | None = None,
) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MetadataError("JSON object contained a duplicate key")
        result[key] = value
    return result


def load_metadata(path: Path) -> object:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise MetadataError("metadata path was not a regular file")
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                MetadataError(f"invalid JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MetadataError("metadata could not be read") from error


class AtomicWriteError(OSError):
    """The replace occurred, but parent persistence could not be confirmed."""

    replaced = True


def atomic_write_metadata(
    path: Path,
    value: Mapping[str, object],
    *,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4()}.tmp"
    descriptor: int | None = None
    replaced = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        payload = (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace(temporary)
        os.replace(temporary, path)
        replaced = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException as error:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()
        if replaced:
            raise AtomicWriteError("ATOMIC_DURABILITY_UNKNOWN") from error
        raise


def is_atomic_temporary_name(name: str, target_name: str) -> bool:
    prefix = f".{target_name}."
    if not name.startswith(prefix):
        return False
    suffix = name.removeprefix(prefix)
    return suffix.endswith(".tmp") and all(
        character.isalnum() or character in {"_", "-"} for character in suffix.removesuffix(".tmp")
    )


def require_real_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MetadataError("directory was unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise MetadataError("path was not a real directory")
    return metadata


def require_regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MetadataError("file was unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MetadataError("path was not a regular file")
    return metadata


def secure_private_directory(
    value: Path,
    *,
    repository_root: Path | None = None,
) -> Path:
    path = value.expanduser().absolute()
    try:
        for part in (*reversed(path.parents), path):
            if part.is_symlink():
                raise MetadataError("symbolic link in private path")
            if not part.exists():
                part.mkdir(mode=PRIVATE_MODE)
                part.chmod(PRIVATE_MODE)
            require_real_directory(part)
        metadata = require_real_directory(path)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != PRIVATE_MODE:
            raise MetadataError("existing private directory must be owner-only")
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise MetadataError("private directory could not be secured") from error
    if repository_root is not None:
        repository = repository_root.resolve()
        if resolved == repository or resolved.is_relative_to(repository):
            raise MetadataError("private directory must be outside the repository")
    return resolved


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if not os.path.lexists(path):
        digest.update(b"<missing>")
        return digest.hexdigest()
    require_real_directory(path)

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise MetadataError("tree could not be inspected") from error
        for child in children:
            child_path = Path(child.path)
            metadata = child.stat(follow_symlinks=False)
            relative = child_path.relative_to(path).as_posix().encode("utf-8")
            if stat.S_ISLNK(metadata.st_mode):
                raise MetadataError("tree contained a symbolic link")
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(relative)
                digest.update(b"\0d\0")
                visit(child_path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise MetadataError("tree contained a non-regular file")
            digest.update(relative)
            digest.update(b"\0f\0")
            try:
                digest.update(child_path.read_bytes())
            except OSError as error:
                raise MetadataError("tree file could not be read") from error
            digest.update(b"\0")

    visit(path)
    return digest.hexdigest()


def files_digest(root: Path, paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise MetadataError("contract path was invalid")
        path = root / relative
        require_regular_file(path)
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as error:
            raise MetadataError("contract file could not be read") from error
        digest.update(b"\0")
    return digest.hexdigest()


def copy_regular_file(source: Path, destination: Path) -> None:
    require_regular_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    require_regular_file(destination)


def remove_owned_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    tree_digest(path)
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise MetadataError("owned tree cleanup left residue")
