"""Composition root for final checkpoint validation and automatic local reporting."""

from hashlib import sha256
import json

from .collection_analysis import read_run, union_ms, stamp
from .collection_state import fingerprint, utc
from .errors import WorkspaceError
from .games.genshin.page import normalize as genshin_normalize
from .games.zzz.page import normalize as zzz_normalize
from .page_snapshot import identity, validate_snapshot
from .storage.common import atomic_write_metadata, load_metadata
from .user_workspace import private_directory
from . import platform_io as io

NORMALIZERS = {"genshin": genshin_normalize, "zzz": zzz_normalize}


def validate_completed(store, *, require_current=True):
    value = store.read()
    if set(value["completed"]) != set(NORMALIZERS):
        raise WorkspaceError("WORKFLOW_CHECKPOINT_MISSING")
    snapshots = {}
    for game, normalize in NORMALIZERS.items():
        entry = value["completed"][game]
        path = io.filesystem_path(store.root / entry["snapshot"])
        if (
            not path.resolve().is_relative_to(io.filesystem_path(store.root / "results").resolve())
            or entry["double_pass"] is not True
            or (
                require_current and entry.get("verified_attempt") != value["attempts"][-1]["number"]
            )
            or sha256(io.read_regular(path, require_private=True)).hexdigest() != entry["sha256"]
        ):
            raise WorkspaceError("WORKFLOW_CHECKPOINT_CHANGED")
        snapshot = load_metadata(path, private=True)
        validate_snapshot(snapshot, game, normalize)
        if fingerprint(identity(snapshot["profile"], game)) != value["pinned"][game]:
            raise WorkspaceError("WORKFLOW_CHECKPOINT_CHANGED")
        snapshots[game] = snapshot
    return snapshots


def make_report(store):
    value = store.read()
    snapshots = validate_completed(store)
    source_ms = overlap_ms = business_overlap_ms = save_ms = source_count = 0
    modes = []
    metrics_complete = True
    for attempt in value["attempts"]:
        folder = store.root / "attempts" / f"{attempt['number']:04d}"
        file = folder / "events.jsonl"
        sources, progress = [], []
        if file.is_file():
            for line in io.read_regular(file, require_private=True).decode().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    metrics_complete = False
                    continue
                if (
                    event.get("kind") == "browser_metric"
                    and event.get("operation") == "source.read"
                ):
                    sources.append(event)
                elif event.get("kind") == "pump":
                    progress.extend(event["events"])
        # The final worker log also includes reports delivered after the last pump.
        if (folder / "result.json").is_file():
            progress = load_metadata(folder / "result.json", private=True)["events"]
        for line in progress:
            if line.startswith("COLLECTION_MODE="):
                mode = line.split("=", 1)[1]
                if mode in {"PARALLEL", "SERIAL", "SERIAL_RECOVERY"} and mode not in modes:
                    modes.append(mode)
            if line.startswith("PAGE_TIMING="):
                timing = json.loads(line.split("=", 1)[1])
                if timing.get("phase") == "save":
                    save_ms += timing["duration_ms"]
        # Browser clocks are comparable only within the same attempt.
        spans = [(r["started"], r["started"] + r["duration_ms"]) for r in sources]
        business = [
            span
            for span, r in zip(spans, sources)
            if r.get("source") in {"page:roster", "page:detail"}
        ]
        active = union_ms(spans)
        source_ms += active
        overlap_ms += sum(b - a for a, b in spans) - active
        business_overlap_ms += sum(b - a for a, b in business) - union_ms(business)
        source_count += len(sources)
        expected_reads = sum(line.startswith("PAGE_SOURCE=") for line in progress)
        metrics_complete &= (
            bool(sources) and len(sources) == expected_reads and attempt["outcome"] == "PASS"
        )
    ended_at = utc()
    games = [
        {
            "game": game,
            "characters": len(data["roster"]),
            "details": len(data["details"]),
            "state": "partial",
            "double_pass": True,
            "identity_reverified": True,
            "collected_attempt": value["completed"][game]["attempt"],
        }
        for game, data in snapshots.items()
    ]
    report = {
        "format": "hoyo-roster-workflow-report-v1",
        "run_id": value["id"],
        "source_sha": value["version"]["source_sha"],
        "package_digest": value["version"]["package_digest"],
        "started_at": value["created_at"],
        "ended_at": ended_at,
        "workflow_seconds": stamp(ended_at) - stamp(value["created_at"]),
        "collection_seconds": sum(
            stamp(a["closed_at"]) - stamp(a["started_at"]) for a in value["attempts"]
        ),
        "attempts": len(value["attempts"]),
        "games": games,
        "source_count_recorded": source_count,
        "collection_modes": modes,
        "login_wait_seconds": sum(
            p["duration_ms"]
            for p in value["phases"]
            if p["phase"] == "WAITING_LOGIN" and p["duration_ms"] is not None
        )
        / 1000,
        "source_active_seconds_recorded": source_ms / 1000,
        "source_overlap_seconds_recorded": overlap_ms / 1000,
        "roster_detail_overlap_seconds_recorded": business_overlap_ms / 1000,
        "save_seconds_recorded": save_ms / 1000,
        "metrics_complete": metrics_complete,
        "evidence_kind": "workflow-execution",
    }
    baseline = value["config"].get("baseline")
    if baseline:
        from pathlib import Path
        from .collection_state import RunStore

        directory = Path(baseline)
        if (directory / "workflow.json").is_file():
            previous_store = RunStore(directory)
            previous_state = previous_store.read()
            if previous_state["state"] != "SUCCEEDED":
                raise WorkspaceError("WORKFLOW_BASELINE_INVALID")
            previous = validate_completed(previous_store)
            old = load_metadata(directory / "report" / "summary.json", private=True)
            seconds = old["collection_seconds"]
        else:
            old, previous = read_run(directory)
            seconds = old["seconds"]
        report["comparison"] = {
            "baseline_seconds": seconds,
            "saved_seconds": seconds - report["collection_seconds"],
            "data_equal": all(
                snapshots[g][k] == previous[g][k]
                for g in NORMALIZERS
                for k in ("profile", "roster", "details", "state", "scope")
            ),
            "same_source": old["source_sha"] == report["source_sha"],
            "scope": "attempt wall time including login waits; setup and report excluded",
        }
    folder = private_directory(store.root / "report")
    atomic_write_metadata(folder / "summary.json", report)
    lines = [
        "# 工作流执行报告",
        "",
        f"完成 {ended_at}；两游戏均通过官方身份核验、双轮与快照验证。",
        "",
        f"全流程墙钟 {report['workflow_seconds']:.3f} 秒；采集尝试墙钟合计 {report['collection_seconds']:.3f} 秒；"
        f"尝试 {report['attempts']} 次。中断和人工等待计入所属阶段。",
        "",
        f"调度模式 {', '.join(modes) or '未记录'}。等待人工登录 {report['login_wait_seconds']:.3f} 秒。",
        f"来源活跃墙钟 {source_ms / 1000:.3f} 秒，其中两游戏名单或详情同时读取 {business_overlap_ms / 1000:.3f} 秒。"
        f"保存耗时合计 {save_ms / 1000:.3f} 秒。",
        "",
        "| 游戏 | 角色 | 详情 | 收集于尝试 |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {g['game']} | {g['characters']} | {g['details']} | {g['collected_attempt']} |"
        for g in games
    ]
    lines += [
        "",
        "结果范围为 OFFICIAL_PAGE / PARTIAL。已提交游戏恢复时只重新核验身份，未提交游戏重做双轮。",
        f"来源计时记录完整：{metrics_complete}。中断时未落盘的来源跨度不会被补造。",
    ]
    if "comparison" in report:
        c = report["comparison"]
        lines += [
            "",
            f"采集对照节省 {c['saved_seconds']:.3f} 秒；数据一致 {c['data_equal']}；源码一致 {c['same_source']}。",
            "不同源码、不同数据或恢复任务的差值不能作为同条件性能结论。",
        ]
    path = folder / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    io.set_private(path, 0o600)
    return report
