import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pumpSlice } from "../src/hoyo_roster/client_runner.mjs";
import { recordCollection, startTimedCollection } from "../tools/timed_collection.mjs";

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function fixture() {
  let pumps = 0, released = 0, cancelled = 0, done = false, question = null, close;
  const events = [];
  const completion = new Promise(resolve => { close = resolve; });
  const status = () => ({ done, question, code: done ? 0 : null, errorCode: null, events: [...events] });
  const task = { status, completion, cancel() {
    cancelled++; done = true; close({ code: 1, errorCode: "LOGIN_CANCELLED", events });
  }, async pump() {
    pumps++;
    if (pumps === 1) question = { attempt: 1 };
    else { events.push("TASK_STATUS=PASS", "CURRENT_RESULT=/private/synthetic-account");
      done = true; close({ code: 0, errorCode: null, events }); }
    return status();
  } };
  return { task, host: { releasePageSettings: async () => { released++; }, drainMetrics: () => [] },
    answer: () => { question = null; }, counts: () => ({ pumps, released, cancelled }) };
}

test("foreground slice returns login immediately and resumes the same task", async () => {
  const f = fixture(), windows = [];
  const first = await pumpSlice(f.task, { onWindow: row => windows.push(row.kind) });
  assert.equal(first.question.attempt, 1);
  assert.equal(f.counts().pumps, 1);
  await sleep(10);
  assert.equal(f.counts().pumps, 1, "no detached continuation");
  f.answer();
  assert.equal((await pumpSlice(f.task)).done, true);
  assert.deepEqual(windows, ["window_begin", "window_end"]);
});

test("slice drains a step that crosses the launch deadline and validates budgets", async () => {
  let reads = 0, active = 0;
  const task = { status: () => ({ done: false }), async pump() {
    active++; reads++; await sleep(25); active--; return { done: false };
  } };
  await pumpSlice(task, { launchMs: 10 });
  assert.equal(active, 0); assert.equal(reads, 1);
  for (const launchMs of [0, -1, Infinity, 180001]) {
    await assert.rejects(pumpSlice(task, { launchMs }), /CLIENT_PROTOCOL_ERROR/);
  }
  assert.equal(reads, 1);
});

test("timed session records steps, suppresses private progress and closes once", async () => {
  const directory = await mkdtemp(join(tmpdir(), "hoyo-recorder-")), f = fixture();
  try {
    const session = await recordCollection(f.host, f.task, directory, { source_sha: "synthetic" }, true);
    assert.equal((await session.advance()).question.attempt, 1);
    f.answer();
    const status = await session.advance();
    assert.equal(status.done, true);
    assert.equal(JSON.stringify(status).includes("synthetic-account"), false);
    assert.equal((await readFile(join(directory, "steps.md"), "utf8")).includes("synthetic-account"), false);
    const result = JSON.parse(await readFile(join(directory, "result.json"), "utf8"));
    assert.equal(result.code, 0);
    assert.equal(result.parallelGames, true);
    await session.advance();
    assert.equal(f.counts().released, 1);
    const rows = (await readFile(join(directory, "events.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
    assert.equal(rows.filter(r => r.kind === "window_begin").length, 2);
    assert.equal(rows.filter(r => r.kind === "pump").length, 2);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("pump/log initialization failure cancels, reaps and restores page settings", async () => {
  for (const initialization of [false, true]) {
    const directory = await mkdtemp(join(tmpdir(), "hoyo-recorder-error-")), f = fixture();
    try {
      if (initialization) await writeFile(join(directory, "steps.md"), "existing");
      f.task.pump = async () => { throw new Error("synthetic failure"); };
      if (initialization) {
        await assert.rejects(recordCollection(f.host, f.task, directory, {}, false));
      } else {
        const session = await recordCollection(f.host, f.task, directory, {}, false);
        await assert.rejects(session.advance(), /synthetic failure/);
        assert.equal(JSON.parse(await readFile(join(directory, "result.json"), "utf8")).code, 1);
      }
      assert.equal(f.counts().cancelled, 1);
      assert.equal(f.counts().released, 1);
    } finally { await rm(directory, { recursive: true, force: true }); }
  }
});

test("preflight failure never touches the browser", async () => {
  const browser = new Proxy({}, { get() { throw new Error("browser touched"); } });
  await assert.rejects(startTimedCollection(browser, { python: "/nonexistent/synthetic-python",
    directory: "/nonexistent/synthetic-run", runtimeRoot: "/nonexistent/runtime" }), /COLLECTION_PREFLIGHT_FAILED/);
});
