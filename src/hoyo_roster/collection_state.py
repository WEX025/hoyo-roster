"""Durable orchestration metadata, separate from authentication and game storage."""

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import time
import uuid

from . import platform_io as io
from .errors import WorkspaceError, WrongAccount
from .storage.common import atomic_write_metadata, load_metadata
from .user_workspace import private_directory

STATES = {
    "PREPARING",
    "COLLECTING",
    "WAITING_BROWSER",
    "WAITING_LOGIN",
    "VERIFYING",
    "REPORTING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def utc():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def fingerprint(role):
    return sha256(f"{role.game}\0{role.uid}\0{role.region}".encode()).hexdigest()


def alive(pid):
    return io.process_alive(pid)


class RunStore:
    def __init__(self, directory):
        self.root = Path(directory).absolute()
        io.check_components(self.root)
        self.path = self.root / "workflow.json"

    @classmethod
    def create(cls, directory, config, owner):
        store = cls(directory)
        if store.root.exists():
            raise WorkspaceError("WORKFLOW_DIRECTORY_EXISTS")
        repository = config.get("repository")
        if repository and (
            store.root.resolve().is_relative_to(Path(repository).resolve())
            or Path(repository).resolve().is_relative_to(store.root.resolve())
        ):
            raise WorkspaceError("UNSAFE_WORKFLOW_DIRECTORY")
        package = Path(config["package_directory"]).resolve()
        if store.root.resolve().is_relative_to(package) or package.is_relative_to(
            store.root.resolve()
        ):
            raise WorkspaceError("UNSAFE_WORKFLOW_DIRECTORY")
        private_directory(store.root)
        with io.exclusive_lock(store.root / "workflow.lock"):
            if store.path.exists():
                raise WorkspaceError("WORKFLOW_DIRECTORY_EXISTS")
            document = {
                "format": "hoyo-roster-workflow-v1",
                "id": str(uuid.uuid4()),
                "created_at": utc(),
                "state": "PREPARING",
                "error_code": None,
                "config": config,
                "owner": owner,
                "version": None,
                "attempts": [],
                "pinned": {},
                "completed": {},
                "phases": [],
                "question": None,
                "preparation_pid": None,
            }
            atomic_write_metadata(store.path, document)
        return store

    @contextmanager
    def edit(self):
        with io.exclusive_lock(self.root / "workflow.lock"):
            value = load_metadata(self.path, private=True)
            if (
                not isinstance(value, dict)
                or value.get("format") != "hoyo-roster-workflow-v1"
                or value.get("state") not in STATES
                or not isinstance(value.get("attempts"), list)
                or not isinstance(value.get("completed"), dict)
                or not isinstance(value.get("pinned"), dict)
            ):
                raise WorkspaceError("WORKFLOW_STATE_INVALID")
            yield value
            atomic_write_metadata(self.path, value)

    def read(self):
        # Reads also take the lock, but never rewrite a checkpoint or its timestamps.
        with io.exclusive_lock(self.root / "workflow.lock"):
            value = load_metadata(self.path, private=True)
            if not isinstance(value, dict) or value.get("state") not in STATES:
                raise WorkspaceError("WORKFLOW_STATE_INVALID")
            return value

    @staticmethod
    def owned(value, token):
        if value["owner"]["token"] != token:
            raise WorkspaceError("WORKFLOW_BUSY")

    def phase(self, value, state, *, error=None):
        if state not in STATES:
            raise WorkspaceError("WORKFLOW_STATE_INVALID")
        now = utc()
        if value["phases"] and value["phases"][-1]["end_at"] is None:
            last = value["phases"][-1]
            last["end_at"] = now
            last["duration_ms"] = (time.time() - last["started_epoch"]) * 1000
        if state not in TERMINAL:
            value["phases"].append(
                {
                    "phase": state,
                    "start_at": now,
                    "end_at": None,
                    "started_epoch": time.time(),
                    "duration_ms": None,
                }
            )
        value["state"], value["error_code"] = state, error
        if state != "WAITING_LOGIN":
            value["question"] = None
        self.write_steps(value)

    def write_steps(self, value):
        lines = [
            "# 工作流阶段计时",
            "",
            "覆盖预检、采集、人工等待、验证与报告。中断空档计入墙钟。",
            "",
            "| 阶段 | 开始 UTC | 结束 UTC | 耗时秒 |",
            "| --- | --- | --- | ---: |",
        ]
        for p in value["phases"]:
            seconds = "进行中" if p["duration_ms"] is None else f"{p['duration_ms'] / 1000:.3f}"
            lines.append(f"| {p['phase']} | {p['start_at']} | {p['end_at'] or ''} | {seconds} |")
        path = self.root / "workflow-steps.md"
        # Metadata remains the authoritative atomic checkpoint if prose writing fails.
        descriptor = io.open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")

    def claim(self, owner):
        with self.edit() as value:
            old = value["owner"]
            if alive(value.get("preparation_pid")):
                raise WorkspaceError("WORKFLOW_PREPARATION_RUNNING")
            if (
                old["token"] != owner["token"]
                and alive(old["pid"])
                and value["state"] not in TERMINAL
            ):
                raise WorkspaceError("WORKFLOW_BUSY")
            if value["attempts"] and alive(value["attempts"][-1].get("worker_pid")):
                raise WorkspaceError("WORKFLOW_WORKER_STILL_RUNNING")
            value["owner"] = owner
            if value["state"] in {"COLLECTING", "WAITING_LOGIN", "WAITING_BROWSER"}:
                if value["attempts"] and value["attempts"][-1]["closed_at"] is None:
                    value["attempts"][-1].update(
                        closed_at=utc(), outcome="INTERRUPTED", worker_pid=None
                    )
                self.phase(value, "PREPARING")
            return value


class CollectionCheckpoint:
    """Called only by the real game path, before reads and after a durable save."""

    def __init__(self, directory):
        self.store = RunStore(directory)
        with self.store.edit() as value:
            if (
                value["state"] != "COLLECTING"
                or not value["attempts"]
                or value["owner"]["pid"] != io.launch_parent_pid()
            ):
                raise WorkspaceError("WORKFLOW_OWNER_MISMATCH")
            from .collection_environment import inventory
            import json

            actual = sha256(
                json.dumps(inventory(Path(__file__).resolve().parent), sort_keys=True).encode()
            ).hexdigest()
            if actual != value["version"]["package_digest"]:
                raise WorkspaceError("WORKFLOW_VERSION_CHANGED")
            attempt = value["attempts"][-1]
            if attempt["closed_at"] is not None or attempt.get("worker_pid") not in {
                None,
                os.getpid(),
            }:
                raise WorkspaceError("WORKFLOW_BUSY")
            attempt["worker_pid"] = os.getpid()
            self.attempt = attempt["number"]

    def pin(self, game, role):
        digest = fingerprint(role)
        with self.store.edit() as value:
            prior = value["pinned"].get(game)
            if prior is not None and prior != digest:
                raise WrongAccount()
            value["pinned"][game] = digest

    def completed(self, game, path, normalize):
        from .page_snapshot import validate_snapshot

        value = self.store.read()
        entry = value["completed"].get(game)
        if entry is None:
            return False
        relative = (
            io.filesystem_path(path).relative_to(io.filesystem_path(self.store.root)).as_posix()
        )
        if (
            relative != entry["snapshot"]
            or sha256(io.read_regular(path, require_private=True)).hexdigest() != entry["sha256"]
        ):
            raise WorkspaceError("WORKFLOW_CHECKPOINT_CHANGED")
        snapshot = load_metadata(path, private=True)
        validate_snapshot(snapshot, game, normalize)
        from .page_snapshot import identity

        if fingerprint(identity(snapshot["profile"], game)) != value["pinned"][game]:
            raise WrongAccount()
        with self.store.edit() as current:
            current["completed"][game]["verified_attempt"] = self.attempt
        return True

    def commit(self, game, path):
        # run_page_game invokes this only after both passes, validation and store.save.
        relative = (
            io.filesystem_path(path).relative_to(io.filesystem_path(self.store.root)).as_posix()
        )
        if not relative.startswith("results/page-observations-v1/"):
            raise WorkspaceError("WORKFLOW_CHECKPOINT_CHANGED")
        digest = sha256(io.read_regular(path, require_private=True)).hexdigest()
        with self.store.edit() as value:
            if value["attempts"][-1]["number"] != self.attempt:
                raise WorkspaceError("WORKFLOW_BUSY")
            value["completed"][game] = {
                "attempt": self.attempt,
                "snapshot": relative,
                "sha256": digest,
                "double_pass": True,
                "committed_at": utc(),
                "verified_attempt": self.attempt,
            }
