import test from "node:test";
import assert from "node:assert/strict";
import { createWorkflowDriver } from "../src/hoyo_roster/client_workflow.mjs";

function fixture() {
  const calls = [], answers = [];
  let state = "PREPARING", attempts = 0, pending = null, done = false, cancelled = 0;
  const snapshot = () => ({ status: { state, done: ["FAILED", "CANCELLED", "SUCCEEDED"].includes(state),
    nextAction: state === "WAITING_LOGIN" ? "CONFIRM_LOGIN" : state === "WAITING_BROWSER" ? "ATTACH_BROWSER" : "CONTINUE",
    question: pending ? { attempt: `1:${pending.attempt}` } : null }, attempt: 1 });
  const control = async (action, extra) => {
    calls.push(action);
    if (action === "prepare") state = "WAITING_BROWSER";
    if (action === "begin") state = "COLLECTING";
    if (action === "observe") state = extra.attempt === null ? "COLLECTING" : "WAITING_LOGIN";
    if (action === "finish") state = "VERIFYING";
    if (action === "verify") state = "REPORTING";
    if (action === "report") state = "SUCCEEDED";
    if (action === "fail") state = "FAILED";
    if (action === "cancel") state = "CANCELLED";
    return snapshot();
  };
  const session = { host: { pendingQuestion: () => pending, answerLogin: async (answer, attempt) => {
    assert.equal(attempt, pending.attempt); answers.push(answer); pending = null;
  } }, async advance() {
    attempts++;
    if (attempts === 1) pending = { attempt: 7 };
    else done = true;
    return { done, question: pending };
  }, async cancel() { cancelled++; done = true; } };
  const driver = createWorkflowDriver({ directory: "/synthetic", initial: snapshot(), control,
    startSession: async () => { calls.push("startSession"); return session; } });
  return { driver, calls, answers, session, counts: () => ({ attempts, cancelled }) };
}

test("one entrypoint prepares, waits, collects, validates and reports in order", async () => {
  const f = fixture();
  assert.equal((await f.driver.advance()).nextAction, "ATTACH_BROWSER");
  assert.equal((await f.driver.advance()).state, "WAITING_BROWSER");
  assert.equal(f.calls.includes("startSession"), false);
  assert.equal((await f.driver.advance({ synthetic: true })).state, "WAITING_LOGIN");
  assert.equal((await f.driver.answerLogin("waiting", "1:7")).state, "WAITING_LOGIN");
  assert.equal(f.answers.length, 0);
  await assert.rejects(f.driver.answerLogin("completed", "0:7"), /WORKFLOW_LOGIN_ATTEMPT_INVALID/);
  await f.driver.answerLogin("completed", "1:7");
  const finished = await f.driver.advance();
  assert.equal(finished.state, "SUCCEEDED");
  assert.ok(finished.report.markdown.endsWith("summary.md"));
  assert.deepEqual(f.calls.filter(x => x !== "observe"),
    ["prepare", "begin", "startSession", "finish", "verify", "report"]);
  assert.equal(f.counts().attempts, 2);
});

test("collection failure cancels and never runs validation/report", async () => {
  const f = fixture();
  f.session.advance = async () => { throw new Error("synthetic private account content"); };
  await f.driver.advance();
  const result = await f.driver.advance({ synthetic: true });
  assert.equal(result.state, "FAILED");
  assert.equal(f.counts().cancelled, 1);
  assert.equal(f.calls.includes("report"), false);
  assert.equal(JSON.stringify(result).includes("private account"), false);
});

test("concurrent advance is rejected and explicit cancellation remains CANCELLED", async () => {
  const f = fixture();
  await f.driver.advance();
  let entered, release;
  const started = new Promise(resolve => { entered = resolve; });
  f.session.advance = () => { entered(); return new Promise(resolve => { release = resolve; }); };
  f.session.cancel = async () => { release({ done: true }); };
  const running = f.driver.advance({ synthetic: true });
  await started;
  await assert.rejects(f.driver.advance(), /WORKFLOW_BUSY/);
  assert.equal((await f.driver.cancel()).state, "CANCELLED");
  assert.equal((await running).state, "CANCELLED");
  assert.equal(f.calls.includes("report"), false);
});

test("a restored verification stage finishes offline without a browser session", async () => {
  const calls = [];
  let state = "VERIFYING";
  const driver = createWorkflowDriver({ directory: "/synthetic",
    initial: { status: { state, done: false } },
    control: async action => { calls.push(action); state = action === "verify" ? "REPORTING" : "SUCCEEDED";
      return { status: { state, done: state === "SUCCEEDED" } }; },
    startSession: async () => { throw new Error("browser must not be touched"); } });
  assert.equal((await driver.advance()).state, "SUCCEEDED");
  assert.deepEqual(calls, ["verify", "report"]);
});
