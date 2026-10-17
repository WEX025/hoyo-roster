"""Durable orchestration executes actual identity, double passes and atomic saves."""

from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from hoyo_roster import cli, collection_environment, platform_io as io
from hoyo_roster.collection_control import dispatch, config_options
from hoyo_roster.collection_environment import inventory
from hoyo_roster.collection_state import RunStore
from hoyo_roster.errors import WorkspaceError
from hoyo_roster.request_gate import RequestGate
from hoyo_roster.storage.common import atomic_write_metadata
from tests.test_page_workflow import PagePort


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(RequestGate, "reserve", reserve)
    config = config_options({"runtimeRoot": str(tmp_path / "runtime")})
    store = RunStore.create(
        tmp_path / "run", config, {"pid": io.launch_parent_pid(), "token": "synthetic"}
    )
    digest = sha256(
        json.dumps(
            inventory(__import__("pathlib").Path(config["package_directory"])), sort_keys=True
        ).encode()
    ).hexdigest()
    with store.edit() as value:
        value["version"] = {"source_sha": "synthetic", "package_digest": digest}
        store.phase(value, "WAITING_BROWSER")
    dispatch({"action": "begin", "directory": str(store.root), "token": "synthetic"})
    return store


def collect(store, port):
    return cli.main(
        [
            "--game",
            "all",
            "--client-stdio",
            "--collection-run",
            str(store.root),
            "--storage-root",
            str(store.root / "results"),
            "--runtime-root",
            store.read()["config"]["runtime_root"],
        ],
        client=port.client(),
    )


def reconnect(store):
    with store.edit() as value:
        value["owner"]["pid"] = 0
        value["attempts"][-1]["worker_pid"] = None  # the interrupted worker has exited
    store.claim({"pid": io.launch_parent_pid(), "token": "synthetic"})
    with store.edit() as value:
        store.phase(value, "WAITING_BROWSER")
    dispatch({"action": "begin", "directory": str(store.root), "token": "synthetic"})


def test_automatic_validation_and_report_require_actual_commits(prepared, capsys):
    assert collect(prepared, PagePort()) == 0
    events = capsys.readouterr().out.splitlines()
    value = prepared.read()
    assert set(value["completed"]) == {"genshin", "zzz"}
    assert all(c["double_pass"] for c in value["completed"].values())
    atomic_write_metadata(
        prepared.root / "attempts/0001/result.json",
        {"code": 0, "errorCode": None, "events": events},
    )
    for action, expected in [
        ("finish", "VERIFYING"),
        ("verify", "REPORTING"),
        ("report", "SUCCEEDED"),
    ]:
        response = dispatch(
            {"action": action, "directory": str(prepared.root), "token": "synthetic"}
        )
        assert response["status"]["state"] == expected
    summary = json.loads((prepared.root / "report/summary.json").read_text())
    assert len(summary["games"]) == 2
    assert summary["metrics_complete"] is False  # no browser metrics fabricated by this test
    assert "123456789" not in json.dumps(summary)
    assert (prepared.root / "workflow-steps.md").is_file()


def test_reconnect_reuses_committed_game_after_fresh_identity_only(prepared):
    first = PagePort()
    first.fail_at[("zzz", "page:roster", 1)] = "SOURCE_DATA_INVALID"
    assert collect(prepared, first) == 1
    assert set(prepared.read()["completed"]) == {"genshin"}
    original = dict(prepared.read()["completed"]["genshin"])
    reconnect(prepared)
    second = PagePort()
    assert collect(prepared, second) == 0
    assert second.counts[("genshin", "page:identity")] == 1
    assert second.counts[("genshin", "page:roster")] == 0
    assert second.counts[("zzz", "page:roster")] == 2
    now = prepared.read()["completed"]["genshin"]
    assert now["sha256"] == original["sha256"] and now["attempt"] == 1
    assert now["verified_attempt"] == 2


@pytest.mark.parametrize("damage", ["account", "snapshot"])
def test_reconnect_stops_on_account_or_snapshot_change_before_more_collection(prepared, damage):
    assert collect(prepared, PagePort()) == 0
    reconnect(prepared)
    if damage == "snapshot":
        file = io.filesystem_path(
            prepared.root / prepared.read()["completed"]["genshin"]["snapshot"]
        )
        value = json.loads(file.read_text())
        value["profile"]["nickname"] = "Changed synthetic"
        atomic_write_metadata(file, value)
    port = PagePort()
    if damage == "account":
        port.uid = "987654321"
    assert collect(prepared, port) == 1
    assert port.counts[("genshin", "page:roster")] == 0
    assert port.counts[("zzz", "page:identity")] == 0


def test_no_resume_while_another_controller_or_worker_is_alive(prepared):
    with pytest.raises(WorkspaceError, match="WORKFLOW_BUSY"):
        prepared.claim({"pid": os.getpid(), "token": "different"})
    with prepared.edit() as value:
        value["owner"]["pid"] = 0
        value["attempts"][-1]["worker_pid"] = os.getpid()
    with pytest.raises(WorkspaceError, match="WORKFLOW_WORKER_STILL_RUNNING"):
        prepared.claim({"pid": os.getpid(), "token": "different"})


def test_failed_verification_cannot_publish_success(prepared):
    with prepared.edit() as value:
        prepared.phase(value, "VERIFYING")
    with pytest.raises(WorkspaceError, match="WORKFLOW_CHECKPOINT_MISSING"):
        dispatch({"action": "verify", "directory": str(prepared.root), "token": "synthetic"})
    assert not (prepared.root / "report/summary.json").exists()


def test_terminal_cancel_and_retry_rules(prepared):
    for action in ["cancel", "cancel"]:
        response = dispatch(
            {"action": action, "directory": str(prepared.root), "token": "synthetic"}
        )
        assert response["status"]["state"] == "CANCELLED"
    dispatch({"action": "retry", "directory": str(prepared.root), "token": "synthetic"})
    dispatch(
        {
            "action": "fail",
            "directory": str(prepared.root),
            "token": "synthetic",
            "error": "WRONG_ACCOUNT",
        }
    )
    with pytest.raises(WorkspaceError, match="WORKFLOW_RETRY_FORBIDDEN"):
        dispatch({"action": "retry", "directory": str(prepared.root), "token": "synthetic"})


def test_prepare_rejects_dirty_source_before_install_or_browser(prepared, monkeypatch):
    # Exercise a source checkout even when the suite imports the installed wheel.
    # Git is mocked below, so this synthetic repository need not exist on disk.
    with prepared.edit() as value:
        value["config"]["repository"] = str(prepared.root.parent / "repository")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return " M synthetic.py\n"

    monkeypatch.setattr(collection_environment, "command", command)
    with pytest.raises(WorkspaceError, match="WORKFLOW_SOURCE_DIRTY"):
        collection_environment.prepare(prepared)
    assert len(calls) == 1 and calls[0][-2:] == ["status", "--porcelain"]


def test_worker_rejects_a_changed_package_before_browser_access(prepared):
    with prepared.edit() as value:
        value["version"]["package_digest"] = "0" * 64
    port = PagePort()
    assert collect(prepared, port) == 1
    assert port.calls == []


def test_preparation_updates_once_repairs_only_when_needed_and_rechecks(
    prepared, tmp_path, monkeypatch
):
    package = tmp_path / "public-package"
    package.mkdir()
    (package / "entry.mjs").write_text("// synthetic public code\n")
    expected = inventory(package)
    with prepared.edit() as value:
        value["version"] = None
        value["config"].update(
            package_directory=str(package),
            repository=str(tmp_path / "repo"),
            update=True,
            installation_artifact=str(tmp_path / "synthetic.whl"),
        )
    commands, installed = [], False

    def command(args, **kwargs):
        nonlocal installed
        commands.append(args)
        if args[0] == "git":
            if args[-4:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
                return "origin/main\n"
            return "synthetic\n" if args[-2:] == ["rev-parse", "HEAD"] else ""
        if "pip" in args:
            environment = kwargs["env"]
            temporary = Path(environment["TEMP"])
            assert temporary == io.filesystem_path(prepared.root / "preparation-temp")
            assert environment["TMP"] == environment["TMPDIR"] == str(temporary)
            assert io.private(temporary)
            installed = True
            return ""
        return json.dumps(expected if installed else {})

    monkeypatch.setattr(collection_environment, "command", command)
    version = collection_environment.prepare(prepared)
    assert version["installation_repaired"]
    assert [c[3] for c in commands if c[0] == "git"] == [
        "status",
        "rev-parse",
        "fetch",
        "merge",
        "rev-parse",
    ]
    assert len([c for c in commands if "pip" in c]) == 1
    with prepared.edit() as value:
        value["version"] = version
    commands.clear()
    assert not collection_environment.prepare(prepared)["installation_repaired"]
    assert all("pip" not in c and "fetch" not in c for c in commands)


def test_worker_rejects_unrelated_launch_parent_before_reading(prepared):
    with prepared.edit() as value:
        value["owner"]["pid"] = os.getpid()
    port = PagePort()
    assert collect(prepared, port) == 1
    assert port.calls == []
