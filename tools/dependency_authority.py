"""Static Runtime and development toolchain dependency authority."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

AUTHORITY_FILENAME = "dependency-authority.toml"
_DEPENDENCY_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PIN_RE = re.compile(
    r"^(?P<name>[a-z][a-z0-9-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*)$"
)
_PYTHON_REQUIREMENT_RE = re.compile(r"^==(?P<major>[1-9][0-9]*)\.(?P<minor>[0-9]+)\.\*$")
_DEV_MARKER_RE = re.compile(r"""^extra\s*==\s*(?P<quote>['"])dev(?P=quote)$""")


class AuthorityError(ValueError):
    """Dependency authority or one of its projections was invalid."""


@dataclass(frozen=True, slots=True)
class PackagePolicy:
    name: str
    path: str
    module: str
    runtime_dependencies: tuple[str, ...]
    internal_dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DependencyAuthority:
    schema_version: int
    requires_python: str
    python_minor: str
    build_backend: str
    build_dependencies: dict[str, str]
    runtime_dependencies: dict[str, str]
    dev_tools: dict[str, str]
    packages: dict[str, PackagePolicy]

    def runtime_document(self) -> dict[str, object]:
        return {
            "build": {
                "backend": self.build_backend,
                "dependencies": self.build_dependencies,
            },
            "packages": {
                name: {
                    "internal-dependencies": list(policy.internal_dependencies),
                    "module": policy.module,
                    "path": policy.path,
                    "runtime-dependencies": list(policy.runtime_dependencies),
                }
                for name, policy in sorted(self.packages.items())
            },
            "python": {"requires": self.requires_python},
            "runtime": {"dependencies": self.runtime_dependencies},
            "schema-version": self.schema_version,
        }

    def dev_document(self) -> dict[str, object]:
        return {
            "dev": {"tools": self.dev_tools},
            "schema-version": self.schema_version,
        }

    def full_document(self) -> dict[str, object]:
        document = self.runtime_document()
        document["dev"] = {"tools": self.dev_tools}
        return document


@dataclass(frozen=True, slots=True)
class PackageMetadata:
    name: str
    path: str
    module: str
    version: str
    requires_python: str
    dependencies: tuple[str, ...]
    dev_dependencies: tuple[str, ...]
    production_document: dict[str, object]

    def distribution_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provides_extra": ["dev"],
            "requires_dist": [
                *self.dependencies,
                *(f"{requirement}; extra == 'dev'" for requirement in self.dev_dependencies),
            ],
            "requires_python": self.requires_python,
            "version": self.version,
        }


def canonical_bytes(document: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise AuthorityError("dependency metadata was not deterministically serializable") from error


def _table(value: object, *, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AuthorityError(f"{label} fields were invalid")
    return value


def _nonempty_string(value: object, *, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AuthorityError(f"{label} was invalid")
    return value


def _dependency_map(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise AuthorityError(f"{label} was invalid")
    result: dict[str, str] = {}
    for raw_name, raw_version in value.items():
        if type(raw_name) is not str or _DEPENDENCY_NAME_RE.fullmatch(raw_name) is None:
            raise AuthorityError(f"{label} dependency name was invalid")
        version = _nonempty_string(raw_version, label=f"{label} version")
        if _PIN_RE.fullmatch(f"{raw_name}=={version}") is None:
            raise AuthorityError(f"{label} version was invalid")
        result[raw_name] = version
    return {name: result[name] for name in sorted(result)}


def _dependency_names(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise AuthorityError(f"{label} was invalid")
    names = tuple(value)
    if (
        len(names) != len(set(names))
        or names != tuple(sorted(names))
        or any(_DEPENDENCY_NAME_RE.fullmatch(name) is None for name in names)
    ):
        raise AuthorityError(f"{label} was invalid")
    return names


def _validate_internal_graph(packages: Mapping[str, PackagePolicy]) -> None:
    def visit(name: str, pending: set[str], complete: set[str]) -> None:
        if name in complete:
            return
        if name in pending:
            raise AuthorityError("internal package dependency graph contained a cycle")
        pending.add(name)
        for dependency in packages[name].internal_dependencies:
            visit(dependency, pending, complete)
        pending.remove(name)
        complete.add(name)

    complete: set[str] = set()
    for package_name in sorted(packages):
        visit(package_name, set(), complete)


def parse_authority(content: bytes) -> DependencyAuthority:
    try:
        decoded = content.decode("utf-8")
        loaded: object = tomllib.loads(decoded)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise AuthorityError("dependency authority TOML was invalid") from error
    root = _table(
        loaded,
        label="dependency authority",
        keys={"schema-version", "python", "build", "runtime", "dev", "packages"},
    )
    schema_version = root["schema-version"]
    if type(schema_version) is not int or schema_version != 1:
        raise AuthorityError("dependency authority schema version was invalid")

    python = _table(root["python"], label="Python authority", keys={"requires"})
    requires_python = _nonempty_string(
        python["requires"],
        label="Python runtime requirement",
    )
    python_match = _PYTHON_REQUIREMENT_RE.fullmatch(requires_python)
    if python_match is None:
        raise AuthorityError("Python runtime requirement must be one exact minor")
    python_minor = f"{python_match.group('major')}.{python_match.group('minor')}"

    build = _table(
        root["build"],
        label="build authority",
        keys={"backend", "dependencies"},
    )
    build_backend = _nonempty_string(build["backend"], label="build backend")
    build_dependencies = _dependency_map(
        build["dependencies"],
        label="build dependency authority",
    )
    runtime = _table(
        root["runtime"],
        label="Runtime authority",
        keys={"dependencies"},
    )
    runtime_dependencies = _dependency_map(
        runtime["dependencies"],
        label="Runtime dependency authority",
    )
    dev = _table(root["dev"], label="development authority", keys={"tools"})
    dev_tools = _dependency_map(dev["tools"], label="development tool authority")
    authority_names = (
        set(build_dependencies),
        set(runtime_dependencies),
        set(dev_tools),
    )
    if any(left & right for index, left in enumerate(authority_names) for right in authority_names[index + 1 :]):
        raise AuthorityError("dependency authority categories overlapped")

    raw_packages = root["packages"]
    if not isinstance(raw_packages, dict) or not raw_packages:
        raise AuthorityError("package policy authority was invalid")
    packages: dict[str, PackagePolicy] = {}
    paths: set[str] = set()
    modules: set[str] = set()
    used_runtime_dependencies: set[str] = set()
    for raw_name, raw_policy in raw_packages.items():
        if type(raw_name) is not str or _DEPENDENCY_NAME_RE.fullmatch(raw_name) is None:
            raise AuthorityError("package policy name was invalid")
        policy_table = _table(
            raw_policy,
            label=f"{raw_name} package policy",
            keys={"path", "module", "runtime-dependencies", "internal-dependencies"},
        )
        path = _nonempty_string(
            policy_table["path"],
            label=f"{raw_name} package path",
        )
        pure_path = PurePosixPath(path)
        if (
            pure_path.is_absolute()
            or ".." in pure_path.parts
            or path != f"skills/{raw_name}"
            or "\\" in path
            or path in paths
        ):
            raise AuthorityError(f"{raw_name} package path was invalid")
        module = _nonempty_string(
            policy_table["module"],
            label=f"{raw_name} module",
        )
        if _MODULE_NAME_RE.fullmatch(module) is None or module in modules:
            raise AuthorityError(f"{raw_name} module was invalid")
        runtime_names = _dependency_names(
            policy_table["runtime-dependencies"],
            label=f"{raw_name} Runtime dependency policy",
        )
        internal_names = _dependency_names(
            policy_table["internal-dependencies"],
            label=f"{raw_name} internal dependency policy",
        )
        unknown_runtime = set(runtime_names) - set(runtime_dependencies)
        if unknown_runtime:
            raise AuthorityError(f"{raw_name} Runtime dependency policy was undeclared")
        used_runtime_dependencies.update(runtime_names)
        paths.add(path)
        modules.add(module)
        packages[raw_name] = PackagePolicy(
            name=raw_name,
            path=path,
            module=module,
            runtime_dependencies=runtime_names,
            internal_dependencies=internal_names,
        )
    if used_runtime_dependencies != set(runtime_dependencies):
        raise AuthorityError("Runtime authority contained an unused dependency")
    for name, policy in packages.items():
        if name in policy.internal_dependencies or any(
            dependency not in packages for dependency in policy.internal_dependencies
        ):
            raise AuthorityError(f"{name} internal dependency policy was invalid")
    _validate_internal_graph(packages)
    return DependencyAuthority(
        schema_version=schema_version,
        requires_python=requires_python,
        python_minor=python_minor,
        build_backend=build_backend,
        build_dependencies=build_dependencies,
        runtime_dependencies=runtime_dependencies,
        dev_tools=dev_tools,
        packages={name: packages[name] for name in sorted(packages)},
    )


def load_authority(path: Path) -> DependencyAuthority:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise AuthorityError("dependency authority could not be read") from error
    return parse_authority(content)


def exact_pin(name: str, version: str) -> str:
    return f"{name}=={version}"


def _pin_name(requirement: str) -> str:
    match = _PIN_RE.fullmatch(requirement)
    if match is None:
        raise AuthorityError("dependency requirement was not an exact pin")
    return match.group("name")


def _exact_pin_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise AuthorityError(f"{label} must contain exact dependency pins")
    requirements = tuple(value)
    matches = tuple(_PIN_RE.fullmatch(requirement) for requirement in requirements)
    if any(match is None for match in matches):
        raise AuthorityError(f"{label} must contain exact dependency pins")
    names = tuple(match.group("name") for match in matches if match is not None)
    if names != tuple(sorted(names)) or len(names) != len(set(names)):
        raise AuthorityError(f"{label} dependency pins were not canonical")
    return requirements


def _module_version(content: bytes, *, package_name: str) -> str:
    try:
        tree = ast.parse(content.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as error:
        raise AuthorityError(f"{package_name} module version source was invalid") from error
    values: list[str] = []
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "__version__"
            and isinstance(statement.value, ast.Constant)
            and type(statement.value.value) is str
        ):
            values.append(statement.value.value)
    if len(values) != 1 or not values[0] or values[0] != values[0].strip():
        raise AuthorityError(f"{package_name} module version was invalid")
    return values[0]


def _expected_hatch_document(module: str) -> dict[str, object]:
    return {
        "build": {
            "targets": {
                "wheel": {
                    "packages": [f"src/{module}"],
                }
            }
        }
    }


def _metadata_table(content: bytes, *, package_name: str) -> dict[str, Any]:
    try:
        loaded: object = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise AuthorityError(f"{package_name} pyproject TOML was invalid") from error
    if not isinstance(loaded, dict):
        raise AuthorityError(f"{package_name} pyproject metadata was invalid")
    return loaded


def validate_package_metadata(
    authority: DependencyAuthority,
    pyprojects: Mapping[str, bytes],
    module_versions: Mapping[str, bytes],
) -> dict[str, PackageMetadata]:
    expected_names = set(authority.packages)
    if set(pyprojects) != expected_names or set(module_versions) != expected_names:
        raise AuthorityError("package metadata projection set did not match the authority")

    parsed: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = {}
    versions: dict[str, str] = {}
    for name, policy in authority.packages.items():
        metadata = _metadata_table(pyprojects[name], package_name=name)
        build = metadata.get("build-system")
        project = metadata.get("project")
        tool = metadata.get("tool")
        if not isinstance(build, dict) or not isinstance(project, dict) or not isinstance(tool, dict):
            raise AuthorityError(f"{name} package metadata tables were invalid")
        if set(build) != {"requires", "build-backend"}:
            raise AuthorityError(f"{name} build metadata fields were invalid")
        declared_name = _nonempty_string(project.get("name"), label=f"{name} project name")
        if declared_name != name:
            raise AuthorityError(f"{name} project name did not match the authority")
        version = _nonempty_string(project.get("version"), label=f"{name} project version")
        source_version = _module_version(module_versions[name], package_name=name)
        if version != source_version:
            raise AuthorityError(f"{name} source package and module versions differed")
        versions[name] = version
        parsed[name] = (metadata, build, project, version)

    result: dict[str, PackageMetadata] = {}
    expected_build_requirements = tuple(
        exact_pin(name, version) for name, version in authority.build_dependencies.items()
    )
    expected_dev = tuple(exact_pin(name, version) for name, version in authority.dev_tools.items())
    for name, policy in authority.packages.items():
        metadata, build, project, version = parsed[name]
        backend = _nonempty_string(build.get("build-backend"), label=f"{name} build backend")
        if backend != authority.build_backend:
            raise AuthorityError(f"{name} build backend did not match the authority")
        build_requirements = _exact_pin_list(
            build.get("requires"),
            label=f"{name} build requirements",
        )
        if build_requirements != expected_build_requirements:
            raise AuthorityError(f"{name} build requirements did not match the authority")
        requires_python = _nonempty_string(
            project.get("requires-python"),
            label=f"{name} Python requirement",
        )
        if requires_python != authority.requires_python:
            raise AuthorityError(f"{name} Python requirement did not match the authority")
        expected_dependencies = tuple(
            sorted(
                [
                    *(
                        exact_pin(dependency, authority.runtime_dependencies[dependency])
                        for dependency in policy.runtime_dependencies
                    ),
                    *(
                        exact_pin(dependency, versions[dependency])
                        for dependency in policy.internal_dependencies
                    ),
                ]
            )
        )
        dependencies = _exact_pin_list(
            project.get("dependencies"),
            label=f"{name} Runtime dependencies",
        )
        if dependencies != expected_dependencies:
            raise AuthorityError(f"{name} Runtime dependencies did not match the authority")
        optional = project.get("optional-dependencies")
        if not isinstance(optional, dict) or set(optional) != {"dev"}:
            raise AuthorityError(f"{name} optional dependency groups were invalid")
        dev_dependencies = _exact_pin_list(
            optional["dev"],
            label=f"{name} development dependencies",
        )
        if dev_dependencies != expected_dev:
            raise AuthorityError(f"{name} development dependencies did not match the authority")
        hatch = metadata["tool"].get("hatch")
        if hatch != _expected_hatch_document(policy.module):
            raise AuthorityError(f"{name} wheel build projection was invalid")
        production_project = {
            key: value for key, value in project.items() if key != "optional-dependencies"
        }
        production_document: dict[str, object] = {
            "build-system": build,
            "project": production_project,
            "tool": {"hatch": hatch},
        }
        canonical_bytes(production_document)
        result[name] = PackageMetadata(
            name=name,
            path=policy.path,
            module=policy.module,
            version=version,
            requires_python=requires_python,
            dependencies=dependencies,
            dev_dependencies=dev_dependencies,
            production_document=production_document,
        )
    return result


def validate_distribution_document(
    contract: PackageMetadata,
    document: Mapping[str, object],
    *,
    include_dev: bool = True,
) -> None:
    required_keys = {"name", "version", "requires_python", "requires_dist", "provides_extra"}
    if set(document) != required_keys:
        raise AuthorityError(f"{contract.name} distribution metadata fields were invalid")
    for key, expected in (
        ("name", contract.name),
        ("version", contract.version),
        ("requires_python", contract.requires_python),
    ):
        if document[key] != expected:
            raise AuthorityError(f"{contract.name} distribution {key} did not match source")
    requires_dist = document["requires_dist"]
    provides_extra = document["provides_extra"]
    if not isinstance(requires_dist, Sequence) or isinstance(requires_dist, (str, bytes)):
        raise AuthorityError(f"{contract.name} distribution requirements were invalid")
    if not isinstance(provides_extra, Sequence) or isinstance(provides_extra, (str, bytes)):
        raise AuthorityError(f"{contract.name} distribution extras were invalid")
    production: list[str] = []
    development: list[str] = []
    for raw_requirement in requires_dist:
        if type(raw_requirement) is not str:
            raise AuthorityError(f"{contract.name} distribution requirement was invalid")
        requirement, separator, marker = raw_requirement.partition(";")
        requirement = requirement.strip()
        if _PIN_RE.fullmatch(requirement) is None:
            raise AuthorityError(f"{contract.name} distribution requirement was not exact")
        if not separator:
            production.append(requirement)
        elif _DEV_MARKER_RE.fullmatch(marker.strip()) is not None:
            development.append(requirement)
        else:
            raise AuthorityError(f"{contract.name} distribution marker was invalid")
    if tuple(sorted(production, key=_pin_name)) != contract.dependencies:
        raise AuthorityError(f"{contract.name} distribution Runtime requirements drifted")
    if (
        include_dev
        and tuple(sorted(development, key=_pin_name)) != contract.dev_dependencies
    ):
        raise AuthorityError(f"{contract.name} distribution development requirements drifted")
    if tuple(sorted(provides_extra)) != ("dev",):
        raise AuthorityError(f"{contract.name} distribution extras drifted")


def expected_installed_versions(
    authority: DependencyAuthority,
    packages: Mapping[str, PackageMetadata],
) -> dict[str, str]:
    versions = {
        name: authority.runtime_dependencies[name]
        for name in sorted(authority.runtime_dependencies)
    }
    versions.update({name: packages[name].version for name in sorted(packages)})
    return versions


def load_repository_projection(
    root: Path,
) -> tuple[DependencyAuthority, dict[str, PackageMetadata]]:
    authority = load_authority(root / AUTHORITY_FILENAME)
    expected_pyprojects = {
        policy.path: name for name, policy in authority.packages.items()
    }
    actual_pyprojects = {
        path.parent.relative_to(root).as_posix(): path
        for path in (root / "skills").glob("*/pyproject.toml")
        if path.is_file()
    }
    if set(actual_pyprojects) != set(expected_pyprojects):
        raise AuthorityError("repository package set did not match the authority")
    pyprojects: dict[str, bytes] = {}
    module_versions: dict[str, bytes] = {}
    try:
        for name, policy in authority.packages.items():
            pyprojects[name] = (root / policy.path / "pyproject.toml").read_bytes()
            module_versions[name] = (
                root / policy.path / "src" / policy.module / "__init__.py"
            ).read_bytes()
    except OSError as error:
        raise AuthorityError("repository package projection could not be read") from error
    return authority, validate_package_metadata(authority, pyprojects, module_versions)
