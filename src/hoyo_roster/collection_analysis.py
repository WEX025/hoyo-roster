"""Replay private collection logs and validate snapshots; never starts a browser."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
from . import platform_io as io

from hoyo_roster.games.genshin.page import normalize as normalize_genshin
from hoyo_roster.games.zzz.page import normalize as normalize_zzz
from hoyo_roster.page_snapshot import validate_snapshot
from hoyo_roster.user_workspace import private_directory, write_json

NORMALIZERS = {"genshin": normalize_genshin, "zzz": normalize_zzz}


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("INVALID_TIME")
    return parsed.timestamp()


def union_ms(spans):
    total, edge = 0, None
    for start, end in sorted(spans):
        if not all(math.isfinite(n) for n in (start, end)) or end < start:
            raise ValueError("INVALID_SPAN")
        total += max(0, end - max(start, edge if edge is not None else start))
        edge = max(end, edge if edge is not None else end)
    return total


def read_run(directory: Path):
    directory = io.filesystem_path(directory)

    def chosen(current, legacy):
        return directory / (current if (directory / current).is_file() else legacy)

    run = json.loads(chosen("result.json", "运行结果.json").read_text(encoding="utf-8"))
    events = run["events"]
    if (
        run["code"] != 0
        or run["errorCode"] is not None
        or events.count("TASK_STATUS=PASS") != 1
        or events.count("GAME_STATUS=PASS") != 2
        or events.count("COMMIT_STATE=COMMITTED") != 2
    ):
        raise ValueError("COLLECTION_NOT_COMMITTED")
    records = [
        json.loads(line)
        for line in chosen("events.jsonl", "实测事件.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    metrics = [row for row in records if row["kind"] == "browser_metric"]
    sources = [row for row in metrics if row["operation"] == "source.read"]
    if not sources or any(row["status"] != "PASS" for row in sources):
        raise ValueError("SOURCE_TIMING_INCOMPLETE")
    spans = [(row["started"], row["started"] + row["duration_ms"]) for row in sources]
    summed = sum(row["duration_ms"] for row in sources)
    united = union_ms(spans)
    clients = [json.loads(e.split("=", 1)[1]) for e in events if e.startswith("CLIENT_TIMING=")]
    starts = sorted(row["started"] for row in clients if row["method"] == "source.read")
    if len(starts) != len(sources):
        raise ValueError("SOURCE_TIMING_INCOMPLETE")
    intervals = [b - a for a, b in zip(starts, starts[1:])]
    # This host-side gate exists in parallel mode. Serial mode uses Python's gate.
    if run["parallelGames"] and intervals and min(intervals) < 1999:
        raise ValueError("SOURCE_START_GATE_VIOLATION")
    windows, gaps, previous = {}, [], None
    for row in records:
        if row["kind"] == "window_begin":
            if row["window"] in windows or (previous is not None and previous[1] is None):
                raise ValueError("UNBALANCED_WINDOWS")
            began = stamp(row["start_at"])
            if previous is not None:
                gaps.append(max(0, began - previous[1]))
            windows[row["window"]] = began
            previous = (row["window"], None)
        elif row["kind"] == "window_end":
            if previous != (row["window"], None):
                raise ValueError("UNBALANCED_WINDOWS")
            previous = (row["window"], stamp(row["end_at"]))
            if previous[1] < windows[row["window"]]:
                raise ValueError("INVALID_TIME")
    if not windows or previous[1] is None:
        raise ValueError("UNBALANCED_WINDOWS")
    operations = defaultdict(lambda: {"count": 0, "seconds": 0})
    for row in metrics:
        if not math.isfinite(row["duration_ms"]) or row["duration_ms"] < 0:
            raise ValueError("INVALID_SPAN")
        item = operations[row["operation"]]
        item["count"] += 1
        item["seconds"] += row["duration_ms"] / 1000
    snapshots, checks = {}, []
    storage = directory / ("results" if (directory / "results").is_dir() else "角色池数据")
    files = list(storage.rglob("snapshot.json"))
    values = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    for game, normalize in NORMALIZERS.items():
        matches = [value for value in values if value.get("game") == game]
        if len(matches) != 1 or events.count(f"PAGE_SOURCE={game}:roster") != 2:
            raise ValueError("DOUBLE_PASS_EVIDENCE_MISSING")
        value = matches[0]
        validate_snapshot(value, game, normalize)
        snapshots[game] = value
        checks.append(
            {
                "game": game,
                "characters": len(value["roster"]),
                "details": len(value["details"]),
                "state": value["state"],
                "scope": value["scope"],
                "roster_reads": 2,
                "identity_reads": events.count(f"PAGE_SOURCE={game}:identity"),
            }
        )
        if checks[-1]["identity_reads"] < 4:
            raise ValueError("IDENTITY_EVIDENCE_MISSING")
    elapsed = stamp(run["closed_at"]) - stamp(run["started_at"])
    if elapsed <= 0:
        raise ValueError("INVALID_TIME")
    return {
        "source_sha": run["source_sha"],
        "parallel_games": run["parallelGames"],
        "seconds": elapsed,
        "source_count": len(sources),
        "source_sum_seconds": summed / 1000,
        "source_union_seconds": united / 1000,
        "source_overlap_seconds": (summed - united) / 1000,
        "minimum_actual_source_start_interval_ms": min(intervals) if intervals else None,
        "window_gaps_seconds": sum(gaps),
        "window_count": len(windows),
        "recovery_events": sum(
            e.startswith(("AUTH_RECOVERY=", "PARALLEL_RECOVERY=")) for e in events
        ),
        "committed_games": 2,
        "checks": checks,
        "operations": dict(operations),
    }, snapshots


def compare_runs(current, baseline):
    now, snapshots = current
    old, previous = baseline
    equal = all(
        snapshots[g][key] == previous[g][key]
        for g in NORMALIZERS
        for key in ("profile", "roster", "details", "state", "scope")
    )
    return {
        "baseline_seconds": old["seconds"],
        "saved_seconds": old["seconds"] - now["seconds"],
        "reduction_percent": (1 - now["seconds"] / old["seconds"]) * 100,
        "same_source": now["source_sha"] == old["source_sha"],
        "data_equal": equal,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        current = read_run(args.run)
        report = {"evidence_kind": "offline-log-replay", "run": current[0]}
        if args.compare:
            report["comparison"] = compare_runs(current, read_run(args.compare))
        output = private_directory(args.output)
        write_json(output / "analysis.json", report)
        run = current[0]
        lines = [
            "# 采集计时复盘",
            "",
            "证据类型：已有日志离线重算；不是新一轮真实采集。",
            "",
            f"源码 `{run['source_sha']}`；总耗时 {run['seconds']:.3f} 秒；两游戏均已提交。",
            "",
            "| 指标 | 结果 |",
            "| --- | ---: |",
            f"| 来源次数 | {run['source_count']} |",
            f"| 来源跨度累计 | {run['source_sum_seconds']:.3f} 秒 |",
            f"| 来源区间并集 | {run['source_union_seconds']:.3f} 秒 |",
            f"| 来源交叠 | {run['source_overlap_seconds']:.3f} 秒 |",
            f"| 窗口衔接空档 | {run['window_gaps_seconds']:.3f} 秒 |",
            "",
            "嵌套操作与不同游戏跨度不能相加当作总耗时。两轮名单和身份事件、快照格式均已检查；页面结果仍为 PARTIAL。",
        ]
        if "comparison" in report:
            c = report["comparison"]
            lines += [
                "",
                f"对照节省 {c['saved_seconds']:.3f} 秒（{c['reduction_percent']:.2f}%）；"
                f"相同源码：{c['same_source']}；数据一致：{c['data_equal']}。",
            ]
        path = output / "analysis.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        from hoyo_roster import platform_io

        platform_io.set_private(path, 0o600)
        print("COLLECTION_ANALYSIS=PASS")
        return 0
    except Exception:
        print("COLLECTION_ANALYSIS=FAIL")
        print("ERROR_CODE=COLLECTION_EVIDENCE_INVALID")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
