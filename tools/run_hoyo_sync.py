#!/usr/bin/env python3
"""Bootstrap and run one fixed Hoyo game routine refresh."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import bootstrap_hoyo_runtime

DEFAULT_EXTERNAL_ROOT = Path("~/.local/share/hoyo-game-data")
_GAME_MODULES = {
    "genshin": "genshin_character_state_sync",
    "hsr": "hsr_character_state_sync",
    "zzz": "zzz_character_state_sync",
}
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSH_AUTH_SOCK",
        "TMPDIR",
    }
)

Bootstrapper = Callable[
    [Path, str, Path],
    tuple[str, bootstrap_hoyo_runtime.RuntimeIdentity],
]
Fetcher = Callable[[Path], None]
ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]

ChildExecutionFailed = bootstrap_hoyo_runtime.ChildExecutionFailed


def _safe_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    values = os.environ if source is None else source
    return {
        key: value
        for key, value in values.items()
        if key.upper() in _ALLOWED_ENVIRONMENT
    }


@contextlib.contextmanager
def _bound_environment(environment: dict[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_hoyo_sync.py")
    parser.add_argument("--repo-root", type=Path, required=True)
    games = parser.add_subparsers(dest="game", required=True)
    for game in ("genshin", "zzz", "hsr"):
        command = games.add_parser(game)
        if game in {"genshin", "hsr"}:
            command.add_argument("--uid", required=True)
        else:
            command.add_argument("--uid")
        browser = command.add_mutually_exclusive_group()
        browser.add_argument("--headed", dest="headed", action="store_true")
        browser.add_argument("--headless", dest="headed", action="store_false")
        command.set_defaults(headed=True)
        command.add_argument("--request-interval", type=float, default=1.8)
    return parser


def run(
    args: argparse.Namespace,
    *,
    bootstrapper: Bootstrapper = bootstrap_hoyo_runtime.bootstrap,
    fetcher: Fetcher = bootstrap_hoyo_runtime.fetch_origin_main,
    runner: ProcessRunner = subprocess.run,
    external_root: Path = DEFAULT_EXTERNAL_ROOT,
) -> int:
    environment = _safe_environment(bootstrap_hoyo_runtime.safe_git_environment())
    with _bound_environment(environment):
        repository, _ = bootstrap_hoyo_runtime.validate_repository(
            args.repo_root,
            "HEAD",
        )
        fetcher(repository)
        disposition, identity = bootstrapper(
            repository,
            "origin/main",
            external_root,
        )
    root = external_root.expanduser().absolute()
    runtime_python = (
        root
        / "runtimes/hoyo-sync/generations"
        / identity.runtime_digest
        / "venv/bin/python"
    )
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        raise bootstrap_hoyo_runtime.RuntimeActivationFailed(
            "active Runtime Python is unavailable"
        )
    game = args.game
    if game not in _GAME_MODULES:
        raise bootstrap_hoyo_runtime.BootstrapError("game is not registered")
    child_arguments = [
        str(runtime_python),
        "-m",
        _GAME_MODULES[game],
        "publish-main",
        "--repo-root",
        str(repository),
        "--headed" if args.headed else "--headless",
        "--request-interval",
        str(args.request_interval),
    ]
    if args.uid is not None:
        child_arguments.extend(("--uid", args.uid))
    child_environment = dict(environment)
    child_environment["PLAYWRIGHT_BROWSERS_PATH"] = str(
        root / "browser/playwright-browsers"
    )
    del disposition
    try:
        child = runner(
            child_arguments,
            cwd=repository,
            env=child_environment,
            check=False,
            text=True,
        )
    except OSError as error:
        raise ChildExecutionFailed("routine child could not be executed") from error
    if child.returncode < 0:
        raise ChildExecutionFailed("routine child terminated without a stable result")
    return child.returncode


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("HOYO_SYNC_LAUNCHER=FAIL", file=sys.stderr)
        print(
            f"ERROR_CODE={bootstrap_hoyo_runtime.LauncherErrorCode.INTERRUPTED.value}",
            file=sys.stderr,
        )
        return 130
    except Exception as error:
        print("HOYO_SYNC_LAUNCHER=FAIL", file=sys.stderr)
        print(
            "ERROR_CODE="
            f"{
                bootstrap_hoyo_runtime.classify_launcher_error(
                    error,
                    fallback=bootstrap_hoyo_runtime.LauncherErrorCode.CHILD_EXECUTION_FAILED,
                ).value
            }",
            file=sys.stderr,
        )
        bootstrap_hoyo_runtime.print_launcher_diagnostics(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
