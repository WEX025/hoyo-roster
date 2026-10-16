// Repository workflow entrypoint. Call only from an active client browser tool.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { writeFile, appendFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRosterHost } from "../src/hoyo_roster/client_runtime.mjs";
import { pumpSlice } from "../src/hoyo_roster/client_runner.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const execute = promisify(execFile);
const fixedProgress = events => events.filter(event =>
  /^(?:PAGE_GAME_BEGIN|PAGE_COLLECTION)=(?:genshin|zzz)$/.test(event) ||
  /^PAGE_SOURCE=(?:genshin|zzz):(?:identity|roster|detail)$/.test(event) ||
  /^PAGE_(?:PASS|DETAILS_DONE)=\d{1,5}$/.test(event) ||
  /^(?:TASK_STATUS|GAME_STATUS)=(?:PASS|FAIL)$/.test(event) ||
  /^COMMIT_STATE=(?:COMMITTED|UNCHANGED|NOT_COMMITTED)$/.test(event));

// Separated from browser construction so failure/cleanup can be tested without
// replacing game identity, normalization, validation or atomic storage logic.
export async function recordCollection(host, task, directory, version, parallelGames) {
  const started_at = new Date().toISOString();
  let seen = 0, sequence = 0, window = 0, busy = false, finishing;
  const log = join(directory, "events.jsonl"), md = join(directory, "steps.md");
  const append = row => appendFile(log, JSON.stringify(row) + "\n");
  const finish = () => finishing ??= (async () => {
    let result;
    try { result = await task.completion; }
    finally { await host.releasePageSettings(); }
    // Include any reports received during process closure, not only the last pump.
    const records = host.drainMetrics().map(metric => ({ kind: "browser_metric", ...metric }));
    for (const record of records) await append(record);
    const closed_at = new Date().toISOString();
    await writeFile(join(directory, "result.json"), JSON.stringify({ ...result, started_at,
      closed_at, source_sha: version.source_sha, parallelGames }, null, 2) + "\n", { flag: "wx", mode: 0o600 });
    await appendFile(md, `\n收尾完成 ${closed_at}；退出码 ${result.code}；总耗时 ${(Date.parse(closed_at) - Date.parse(started_at)) / 1000} 秒。\n`);
    return result;
  })();
  try {
    await writeFile(join(directory, "version.json"), JSON.stringify(version, null, 2) + "\n", { flag: "wx", mode: 0o600 });
    await writeFile(log, "", { flag: "wx", mode: 0o600 });
    await writeFile(md, `# 采集逐步计时\n\n源码 ${version.source_sha}；并发 ${parallelGames}；开始 ${started_at}。\n\n| 步骤 | 开始 UTC | 耗时秒 | 固定进度 |\n| --- | --- | ---: | --- |\n`, { flag: "wx", mode: 0o600 });
  } catch (error) {
    task.cancel();
    try { await task.completion; } finally { await host.releasePageSettings(); }
    throw error;
  }
  return {
    host,
    async advance(options = {}) {
      if (busy) throw new Error("CLIENT_PROTOCOL_ERROR");
      busy = true;
      const previous = seen;
      try {
        const status = await pumpSlice(task, { launchMs: options.launchMs ?? 180000,
          onWindow: async row => {
            if (row.kind === "window_begin") window++;
            await append({ ...row, window });
          },
          onStep: async ({ started, duration_ms, status }) => {
            const events = status.events.slice(seen); seen = status.events.length;
            for (const metric of host.drainMetrics()) await append({ kind: "browser_metric", ...metric });
            const row = { kind: "pump", id: ++sequence, window,
              start_at: new Date(performance.timeOrigin + started).toISOString(),
              end_at: new Date().toISOString(), duration_ms, events };
            await append(row);
            await appendFile(md, `| ${sequence} | ${row.start_at} | ${(duration_ms / 1000).toFixed(3)} | ${fixedProgress(events).join("；") || "推进"} |\n`);
          } });
        if (status.done) await finish();
        return { done: status.done, code: status.code, errorCode: status.errorCode,
          question: status.question, window, progress: fixedProgress(status.events.slice(previous)).slice(-6) };
      } catch (error) {
        task.cancel();
        try { await finish(); } catch { /* Original failure remains authoritative. */ }
        throw error;
      } finally { busy = false; }
    },
    async cancel() { task.cancel(); return finish(); },
  };
}

export async function startTimedCollection(browser, { python, directory, runtimeRoot, parallelGames = false }) {
  if (typeof python !== "string" || !directory || !runtimeRoot || typeof parallelGames !== "boolean") {
    throw new Error("INVALID_COLLECTION_OPTIONS");
  }
  directory = resolve(directory);
  let version;
  try {
    const { stdout } = await execute(python, [join(root, "tools/verify_installation.py"),
      "--prepare-run", directory], { cwd: root, timeout: 30000, maxBuffer: 16384 });
    version = JSON.parse(stdout);
    if (!version.source_worktree_clean || !version.installation_matches_source || !version.source_sha) throw new Error();
  } catch { throw new Error("COLLECTION_PREFLIGHT_FAILED"); }
  const host = createRosterHost(browser);
  const task = host.startTask({ python, cwd: root, game: "all", operation: "sync",
    parallelGames, storageRoot: join(directory, "results"), runtimeRoot: resolve(runtimeRoot) });
  return recordCollection(host, task, directory, version, parallelGames);
}
