"""Offline evidence analysis uses actual normalization/store and synthetic accounts."""

import json
import shutil
import subprocess

import pytest

from hoyo_roster import cli
from hoyo_roster.request_gate import RequestGate
from tests.test_page_workflow import PagePort, arguments
from tools.analyze_collection import compare_runs, read_run, union_ms
from tools.verify_installation import verify
from tools import verify_installation


@pytest.fixture
def run_dir(tmp_path, monkeypatch, capsys):
    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(RequestGate, "reserve", reserve)
    directory = tmp_path / "run"
    assert cli.main(arguments(directory), client=PagePort().client()) == 0
    events = capsys.readouterr().out.splitlines()
    spans = [(0, 3000), (2000, 3000), (6000, 3000), (8000, 2000)]
    records = [{"kind": "window_begin", "window": 1, "start_at": "2026-01-01T00:00:00Z"}]
    for start, duration in spans:
        records.append(
            {
                "kind": "browser_metric",
                "operation": "source.read",
                "status": "PASS",
                "started": start,
                "duration_ms": duration,
            }
        )
        events.append("CLIENT_TIMING=" + json.dumps({"method": "source.read", "started": start}))
    records.append({"kind": "window_end", "window": 1, "end_at": "2026-01-01T00:00:10Z"})
    (directory / "events.jsonl").write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    result = {
        "code": 0,
        "errorCode": None,
        "events": events,
        "source_sha": "synthetic",
        "parallelGames": True,
        "started_at": "2026-01-01T00:00:00Z",
        "closed_at": "2026-01-01T00:00:10Z",
    }
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return directory


def test_replay_measures_overlap_without_adding_nested_metrics(run_dir):
    summary, snapshots = read_run(run_dir)
    assert summary["source_sum_seconds"] == 11
    assert summary["source_union_seconds"] == 9
    assert summary["source_overlap_seconds"] == 2
    assert summary["seconds"] == 10
    assert summary["committed_games"] == 2
    assert set(snapshots) == {"genshin", "zzz"}
    assert all(c["identity_reads"] >= 4 and c["roster_reads"] == 2 for c in summary["checks"])
    assert "123456789" not in json.dumps(summary)


def test_comparison_detects_account_change_despite_same_timing(run_dir, tmp_path):
    original = read_run(run_dir)
    copied = tmp_path / "copy"
    shutil.copytree(run_dir, copied)
    snapshot = next((copied / "results").rglob("snapshot.json"))
    value = json.loads(snapshot.read_text())
    value["profile"]["uid"] = "987654321"
    snapshot.write_text(json.dumps(value), encoding="utf-8")
    comparison = compare_runs(read_run(copied), original)
    assert not comparison["data_equal"]
    assert comparison["same_source"]
    assert comparison["saved_seconds"] == 0


@pytest.mark.parametrize(
    "damage", ["failed", "one-pass", "identity", "missing-details", "gate", "window"]
)
def test_incomplete_or_invalid_evidence_is_rejected(run_dir, damage):
    path = run_dir / "result.json"
    result = json.loads(path.read_text())
    if damage == "failed":
        result["code"] = 1
    elif damage == "one-pass":
        result["events"].remove("PAGE_SOURCE=genshin:roster")
    elif damage == "identity":
        result["events"] = [e for e in result["events"] if e != "PAGE_SOURCE=genshin:identity"]
    elif damage == "gate":
        timings = [i for i, e in enumerate(result["events"]) if e.startswith("CLIENT_TIMING=")]
        result["events"][timings[1]] = 'CLIENT_TIMING={"method":"source.read","started":1}'
    elif damage == "missing-details":
        snapshot = next((run_dir / "results").rglob("snapshot.json"))
        value = json.loads(snapshot.read_text())
        value["details"] = []
        snapshot.write_text(json.dumps(value), encoding="utf-8")
    else:
        log = run_dir / "events.jsonl"
        log.write_text(log.read_text().splitlines()[0], encoding="utf-8")
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(Exception):
        read_run(run_dir)


def test_union_rejects_invalid_timing():
    assert union_ms([(0, 10), (2, 4), (9, 11), (12, 20)]) == 19
    with pytest.raises(ValueError):
        union_ms([(1, float("nan"))])


def test_installation_checks_inventory_and_bytes(tmp_path, monkeypatch, capsys):
    root, installed = tmp_path / "repo", tmp_path / "installed"
    source = root / "src/hoyo_roster"
    source.mkdir(parents=True)
    source.joinpath("entry.mjs").write_text("synthetic", encoding="utf-8")
    shutil.copytree(source, installed)
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-qm",
            "synthetic",
        ],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    assert verify(root, installed)["installation_matches_source"]
    assert verify(root, installed)["source_worktree_clean"]
    monkeypatch.setattr(verify_installation, "ROOT", root)
    monkeypatch.setattr(verify_installation.hoyo_roster, "__file__", str(installed / "__init__.py"))
    import sys

    for path, expected in [
        (tmp_path / "private-run", 0),
        (tmp_path / "private-run", 1),
        (tmp_path / "unused" / ".." / "repo" / "nested", 1),
    ]:
        monkeypatch.setattr(sys, "argv", ["verify_installation.py", "--prepare-run", str(path)])
        assert verify_installation.main() == expected
        result = json.loads(capsys.readouterr().out)
        assert result["installation_matches_source"]
    assert not (root / "nested").exists()
    installed.joinpath("entry.mjs").write_text("stale", encoding="utf-8")
    installed.joinpath("extra.mjs").write_text("unexpected", encoding="utf-8")
    result = verify(root, installed)
    assert result["mismatched_files"] == result["unexpected_installed_files"] == 1
    assert not result["installation_matches_source"]
