// One public entrypoint. Browser operations remain inside awaited advance calls.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { access } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { platform } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const executeFile = promisify(execFile);
const execute = (file, args, options = {}) => executeFile(file, args, { ...options, windowsHide: true });
const moduleDirectory = dirname(fileURLToPath(import.meta.url));
const live = globalThis[Symbol.for("hoyo-roster.workflows")] ??= new Map();
const failure = code => Object.assign(new Error(code), { code });
const safeCode = error => /^[A-Z][A-Z_]{1,80}$/.test(error?.code ?? error?.message ?? "") ? (error.code ?? error.message) : "WORKFLOW_EXECUTION_FAILED";

async function selectPython(selected) {
  const venv = join(dirname(dirname(moduleDirectory)), ".venv", platform() === "win32" ? "Scripts/python.exe" : "bin/python");
  const installedVenv = dirname(moduleDirectory).endsWith("site-packages")
    ? join(moduleDirectory, ...(platform() === "win32" ? ["..", "..", "..", "Scripts", "python.exe"] : ["..", "..", "..", "..", "bin", "python"])) : null;
  const candidates = selected ? [selected] : [installedVenv, venv, "python3.12", "python"].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const { stdout } = await execute(candidate, ["-I", "-c", "import sys; print(int(sys.version_info[:2] == (3,12)))"], { timeout: 10000 });
      if (stdout.trim() === "1") return candidate;
    } catch { /* Try only known interpreter candidates, never search user directories. */ }
  }
  throw failure("WORKFLOW_PYTHON_312_REQUIRED");
}

async function ensureDependencies(python, options) {
  try {
    await execute(python, ["-I", "-c", "import portalocker,platformdirs,jsonschema"], { timeout: 10000 });
    return;
  } catch { /* A bare interpreter may bootstrap from the selected public package. */ }
  if (options.repairInstallation === false) throw failure("WORKFLOW_DEPENDENCIES_REQUIRED");
  const repository = dirname(moduleDirectory).endsWith("src") ? dirname(dirname(moduleDirectory)) : null;
  const target = options.installationArtifact ?? repository;
  if (!target) throw failure("WORKFLOW_INSTALLATION_SOURCE_REQUIRED");
  if (repository) {
    const { stdout } = await execute("git", ["status", "--porcelain"], { cwd: repository, timeout: 5000 });
    if (stdout.trim()) throw failure("WORKFLOW_SOURCE_DIRTY");
  }
  try {
    await execute(python, ["-m", "pip", "install", "--no-cache-dir", target], { timeout: 180000, maxBuffer: 65536 });
  } catch { throw failure("WORKFLOW_ENVIRONMENT_INSTALL_FAILED"); }
}

export function createWorkflowDriver({ directory, control, startSession, initial }) {
  let data = initial, session = null, browser = null, inFlight = null, cancelling = false;
  const view = () => ({ ...data.status,
    nextAction: data.status.state === "WAITING_BROWSER" && browser ? "CONTINUE" : data.status.nextAction,
    ...(data.status.state === "SUCCEEDED" ? { report: { json: join(directory, "report/summary.json"), markdown: join(directory, "report/summary.md") } } : {}) });
  const call = async (action, extra = {}) => { data = await control(action, extra); return data; };
  async function advance(attachedBrowser, { launchMs = 180000 } = {}) {
    if (inFlight || cancelling) throw failure("WORKFLOW_BUSY");
    if (!Number.isFinite(launchMs) || launchMs <= 0 || launchMs > 180000) throw failure("WORKFLOW_OPTIONS_INVALID");
    if (attachedBrowser) {
      if (browser && browser !== attachedBrowser) throw failure("WORKFLOW_BROWSER_CHANGED");
      browser = attachedBrowser;
    }
    const active = (async () => {
      try {
        if (data.status.done) return view();
        if (data.status.state === "PREPARING") {
          await call("prepare");
          return view(); // Leave a full foreground budget for collection next time.
        }
        if (data.status.state === "WAITING_BROWSER") {
          if (!browser) return view();
          await call("begin");
          session = await startSession(data, browser);
        }
        if (["COLLECTING", "WAITING_LOGIN"].includes(data.status.state)) {
          if (!session) throw failure("WORKFLOW_RECONNECT_REQUIRED");
          const state = await session.advance({ launchMs });
          if (cancelling) { await call("cancel"); return view(); }
          if (state.done) await call("finish");
          else await call("observe", { attempt: state.question?.attempt ?? null });
        }
        if (cancelling) { await call("cancel"); return view(); }
        if (data.status.state === "VERIFYING") await call("verify");
        if (data.status.state === "REPORTING") await call("report");
        return view();
      } catch (error) {
        if (session) { try { await session.cancel(); } catch { /* Preserve original failure. */ } }
        try { await call(cancelling ? "cancel" : "fail", { error: safeCode(error) }); }
        catch { throw failure("WORKFLOW_CHECKPOINT_WRITE_FAILED"); }
        return view();
      }
    })();
    inFlight = active;
    try { return await active; } finally { inFlight = null; }
  }
  return {
    advance,
    async status() { if (!inFlight) await call("inspect"); return view(); },
    async retry() {
      if (inFlight || cancelling) throw failure("WORKFLOW_BUSY");
      await call("retry"); session = null;
      return view();
    },
    async answerLogin(answer, attempt) {
      if (inFlight || data.status.state !== "WAITING_LOGIN" || data.status.question?.attempt !== attempt ||
          !["completed", "waiting", "cancel"].includes(answer)) throw failure("WORKFLOW_LOGIN_ATTEMPT_INVALID");
      if (answer === "cancel") return this.cancel();
      if (answer === "waiting") return view(); // Keep the same suspended question; no repeated prompt.
      const pending = session?.host.pendingQuestion();
      if (!pending || `${data.attempt}:${pending.attempt}` !== attempt) throw failure("WORKFLOW_LOGIN_ATTEMPT_INVALID");
      await session.host.answerLogin("completed", pending.attempt);
      await call("observe", { attempt: null });
      return view();
    },
    async cancel() {
      if (data.status.done) return view();
      cancelling = true;
      try {
        if (session) await session.cancel();
        if (inFlight) await inFlight;
        await call("cancel");
        return view();
      } finally { cancelling = false; }
    },
  };
}

export function createWorkflowControl(python, directory, owner) {
  const bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('hoyo_roster.collection_control',run_name='__main__')";
  return async (action, extra = {}) => {
    const request = { action, directory, token: owner.token, ...extra };
    const pending = execute(python, ["-I", "-c", bootstrap, dirname(moduleDirectory)], { timeout: 210000, maxBuffer: 1048576 });
    pending.child.stdin.on("error", () => {});
    pending.child.stdin.end(JSON.stringify(request));
    let stdout;
    try { ({ stdout } = await pending); } catch (error) { stdout = error.stdout; }
    let result;
    try { result = JSON.parse(stdout); } catch { throw failure("WORKFLOW_CONTROL_UNAVAILABLE"); }
    if (!result.ok) throw failure(result.errorCode);
    return result;
  };
}

export async function resolveWorkflowOwner(python) {
  // No process global is required in the client sandbox. The platform layer
  // recognizes the one verified Windows venv redirector on both launch paths.
  const probe = "import sys; sys.path.insert(0,sys.argv[1]); from hoyo_roster.platform_io import launch_parent_pid; print(launch_parent_pid())";
  const { stdout } = await execute(python, ["-I", "-c", probe, dirname(moduleDirectory)], { timeout: 10000 });
  const owner = { pid: Number(stdout.trim()), token: randomUUID() };
  if (!Number.isSafeInteger(owner.pid) || owner.pid <= 0) throw failure("WORKFLOW_OWNER_UNAVAILABLE");
  return owner;
}

export async function openCollectionWorkflow({ directory, python: selectedPython, ...options }) {
  const started = performance.now(), start_at = new Date().toISOString();
  if (typeof directory !== "string" || !directory || directory.includes("\0")) throw failure("WORKFLOW_OPTIONS_INVALID");
  const allowed = new Set(["parallelGames", "runtimeRoot", "update", "repairInstallation", "installationArtifact", "baseline"]);
  if (Object.keys(options).some(key => !allowed.has(key)) ||
      ["parallelGames", "update", "repairInstallation"].some(key => options[key] !== undefined && typeof options[key] !== "boolean") ||
      ["runtimeRoot", "installationArtifact", "baseline"].some(key => options[key] !== undefined && (typeof options[key] !== "string" || !options[key] || options[key].includes("\0"))) ||
      (selectedPython !== undefined && (typeof selectedPython !== "string" || !selectedPython || selectedPython.includes("\0")))) throw failure("WORKFLOW_OPTIONS_INVALID");
  directory = resolve(directory);
  if (live.has(directory)) {
    if (Object.keys(options).length) throw failure("WORKFLOW_OPTIONS_IMMUTABLE");
    return live.get(directory);
  }
  let exists = false;
  try { await access(join(directory, "workflow.json")); exists = true; } catch { /* New run. */ }
  if (exists && Object.keys(options).length) throw failure("WORKFLOW_OPTIONS_IMMUTABLE");
  const python = await selectPython(selectedPython);
  await ensureDependencies(python, options);
  const owner = await resolveWorkflowOwner(python);
  const control = createWorkflowControl(python, directory, owner);
  const initial = await control(exists ? "claim" : "create", exists ? { owner } : { owner, options,
    bootstrap: { start_at, end_at: new Date().toISOString(), duration_ms: performance.now() - started } });
  const startSession = async (data, browser) => {
    const scripts = data.version.scripts;
    const { createRosterHost } = await import(pathToFileURL(join(scripts, "client_runtime.mjs")));
    const { recordCollection } = await import(pathToFileURL(join(scripts, "client_collection.mjs")));
    const host = createRosterHost(browser);
    const task = host.startTask({ python: data.config.python, cwd: data.config.repository ?? moduleDirectory,
      game: "all", operation: "sync", parallelGames: data.config.parallel_games,
      storageRoot: join(directory, "results"), runtimeRoot: data.config.runtime_root, collectionRun: directory });
    return recordCollection(host, task, join(directory, "attempts", String(data.attempt).padStart(4, "0")),
      data.version, data.config.parallel_games);
  };
  const workflow = createWorkflowDriver({ directory, control, startSession, initial });
  live.set(directory, workflow);
  return workflow;
}
