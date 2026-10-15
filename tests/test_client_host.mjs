import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm, realpath, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createClientHost } from "../src/hoyo_roster/client_host.mjs";

const root = resolve(import.meta.dirname, "..");
const python = join(root, ".venv", process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "python.exe" : "python");
const fixtures = JSON.parse(await readFile(new URL("./client-sources.json", import.meta.url), "utf8"));

function browserFixture() {
  const events = [];
  const page = {
    id: "same-page",
    goto: async url => events.push(["goto", url]),
    markHandoff: async () => events.push(["retain"]),
  };
  let present = true;
  const browser = {
    tabs: {
      list: async () => present ? [{ id: page.id, url: "https://act.mihoyo.com/ys/event/calculator/index.html#/character" }] : [],
      get: async id => { assert.equal(id, page.id); return page; },
      new: async () => { throw new Error("Existing login tab must be reused"); },
    },
    capabilities: { list: async () => [] },
  };
  return { browser, page, events, removePage: () => { present = false; } };
}

function request(id, method, session, params = {}) {
  return { version: 1, id, method, session, params };
}

test("A browser identifier is not a live browser connection", () => {
  assert.throws(() => createClientHost("browser-2"), /CLIENT_BROWSER_REQUIRED/);
});

test("Host without a source reader advertises the limitation explicitly", async () => {
  const fixture = browserFixture();
  const host = createClientHost(fixture.browser);
  const attached = await host.exchange(request(1, "session.attach", null));
  assert.equal(attached.result.source_reader, false);
  const result = await host.exchange(request(2, "source.read", attached.session,
    { game: "zzz", source: "binding", params: {}, body: null }));
  assert.equal(result.error, "CLIENT_SOURCE_UNAVAILABLE");
  assert.deepEqual(fixture.events, []);
});

test("Login confirmation retains the same page and performs no data reads", async () => {
  const fixture = browserFixture();
  let reads = 0;
  const host = createClientHost(fixture.browser, { readSource: async () => { reads++; } });
  const attach = await host.exchange(request(1, "session.attach", null));
  const opened = await host.exchange(request(2, "login.open", attach.session,
    { url: "https://act.mihoyo.com/ys/event/calculator/index.html#/character" }));
  const confirmation = host.exchange(request(3, "login.confirm", attach.session,
    { page: opened.result.page, question: "是否完成登录？" }));
  for (let n = 0; n < 100 && !host.pendingQuestion(); n++) await new Promise(r => setTimeout(r, 1));
  assert.ok(host.pendingQuestion());
  await host.answerLogin("completed", host.pendingQuestion().attempt);
  assert.equal((await confirmation).result, "completed");
  assert.equal(reads, 0);
  assert.ok(fixture.events.every(([event]) => event === "retain"));
});

test("Closing the login page produces a terminal page error without replacing it", async () => {
  const fixture = browserFixture();
  const host = createClientHost(fixture.browser);
  const attach = await host.exchange(request(1, "session.attach", null));
  const opened = await host.exchange(request(2, "login.open", attach.session,
    { url: "https://act.mihoyo.com/ys/event/calculator/index.html#/character" }));
  fixture.removePage();
  const result = await host.exchange(request(3, "login.confirm", attach.session,
    { page: opened.result.page, question: "是否完成登录？" }));
  assert.equal(result.error, "CLIENT_PAGE_UNAVAILABLE");
});

test("Session changes and unregistered source operations are rejected", async () => {
  const fixture = browserFixture();
  const host = createClientHost(fixture.browser);
  const attach = await host.exchange(request(1, "session.attach", null));
  assert.equal((await host.exchange(request(2, "login.open", "wrong", {}))).error, "CLIENT_PROTOCOL_ERROR");
  assert.equal((await host.exchange(request(2, "source.read", attach.session,
    { game: "zzz", source: "unregistered" }))).error, "CLIENT_PROTOCOL_ERROR");
});

test("Production worker performs user confirmation, both games, validation and saving through the host", { timeout: 120000 }, async () => {
  const fixture = browserFixture();
  const temporary = await realpath(await mkdtemp(join(tmpdir(), "hoyo-client-pipe-")));
  let authenticated = false;
  const reads = [];
  const host = createClientHost(fixture.browser, { readSource: async query => {
    assert.equal(query.browser, fixture.browser);
    if (!authenticated) {
      const error = new Error("AUTHENTICATION_REQUIRED");
      error.code = "AUTHENTICATION_REQUIRED";
      throw error;
    }
    reads.push([query.game, query.source]);
    return fixtures[query.game][query.source];
  } });
  const task = host.startTask({ python, cwd: root,
    storageRoot: join(temporary, "results"), runtimeRoot: join(temporary, "runtime") });
  try {
    for (let n = 0; n < 1000 && !host.pendingQuestion() && !task.status().done; n++) {
      await task.pump({waitMs: 25});
    }
    assert.ok(host.pendingQuestion(), JSON.stringify(task.status()));
    authenticated = true; // Represents the person completing the browser login.
    await host.answerLogin("completed", host.pendingQuestion().attempt);
    while (!task.status().done) await task.pump({waitMs: 100});
    const result = await task.completion;
    assert.equal(result.code, 0, result.events.join("\n"));
    assert.equal(reads.filter(([g,s]) => g === "genshin" && s === "cultivation-avatar-list").length, 2);
    assert.equal(reads.filter(([g,s]) => g === "zzz" && s === "record:avatar/basic").length, 2);
    assert.equal(result.events.filter(e => e === "PROGRESS=LOGIN_COMPLETED").length, 1);
    assert.ok(result.events.includes("TASK_STATUS=PASS"));
    assert.equal(result.events.filter(e => e.startsWith("CURRENT_RESULT=")).length, 2);
  } finally {
    if (!task.status().done) { task.cancel(); await task.completion; }
    await rm(temporary, { recursive: true, force: true });
  }
});

test("Process callbacks never use browser tools outside an explicit pump call", { timeout: 15000 }, async () => {
  const fixture = browserFixture();
  const originalList = fixture.browser.tabs.list;
  let insideTool = false;
  let browserCalls = 0;
  fixture.browser.tabs.list = async () => {
    assert.ok(insideTool, "Browser tool invoked from a background callback");
    browserCalls++;
    return originalList();
  };
  const temporary = await realpath(await mkdtemp(join(tmpdir(), "hoyo-client-pump-")));
  const host = createClientHost(fixture.browser);
  const task = host.startTask({ python, cwd: root, operation: "login",
    runtimeRoot: join(temporary, "runtime") });
  try {
    for (let i=0;i<100 && !task.status().pendingRequests;i++) await new Promise(r=>setTimeout(r,10));
    assert.equal(browserCalls, 0);
    assert.ok(task.status().pendingRequests);
    for (let i=0;i<100 && !host.pendingQuestion();i++) {
      insideTool = true;
      try { await task.pump({waitMs:25}); } finally { insideTool = false; }
    }
    assert.ok(host.pendingQuestion());
    insideTool = true;
    try { await host.answerLogin("cancel", host.pendingQuestion().attempt); } finally { insideTool = false; }
    while (!task.status().done) {
      insideTool = true;
      try { await task.pump({waitMs:25}); } finally { insideTool = false; }
    }
    const result = await task.completion;
    assert.equal(result.code, 1);
    assert.ok(result.events.includes("ERROR_CODE=LOGIN_CANCELLED"));
  } finally {
    if (!task.status().done) { task.cancel(); await task.completion; }
    await rm(temporary,{recursive:true,force:true});
  }
});

test("Concurrent tasks cannot borrow the same browser even with different metadata roots", {timeout:15000}, async () => {
  const fixture=browserFixture();
  const temporary=await realpath(await mkdtemp(join(tmpdir(),"hoyo-client-lease-")));
  const first=createClientHost(fixture.browser);
  const second=createClientHost(fixture.browser);
  const task=first.startTask({python,cwd:root,runtimeRoot:join(temporary,"one")});
  try {
    assert.throws(()=>second.startTask({python,cwd:root,runtimeRoot:join(temporary,"two")}),/USER_WORKSPACE_BUSY/);
  } finally {
    task.cancel();
    await task.completion;
    await rm(temporary,{recursive:true,force:true});
  }
});

function deadline(promise, ms = 5000) {
  let timer;
  return Promise.race([promise, new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error("Operation failed to terminate")), ms);
  })]).finally(() => clearTimeout(timer));
}

async function pumpUntil(task, predicate) {
  for (let n = 0; n < 400 && !predicate() && !task.status().done; n++) await task.pump({waitMs: 25});
  assert.ok(predicate(), JSON.stringify(task.status()));
}

test("Invalid startup cannot leak a browser lease or coerce caller objects", async () => {
  const {browser} = browserFixture();
  let coerced = false;
  for (const invalid of [null, {python:""}, {cwd:"relative"}, {python:"bad\0path"},
    {uid:{toString() { coerced = true; return "123"; }}}]) {
    const options = invalid === null ? null : {python,cwd:root,...invalid};
    assert.throws(() => createClientHost(browser).startTask(options), /CLIENT_PROTOCOL_ERROR/);
  }
  assert.equal(coerced, false);
  const task = createClientHost(browser).startTask({python,cwd:root,operation:"login"});
  task.cancel();
  assert.equal((await deadline(task.completion)).errorCode, "LOGIN_CANCELLED");
});

test("Failed process launch releases its lease and reports a fixed error", async () => {
  const {browser} = browserFixture();
  const failed = createClientHost(browser).startTask({python:join(root,"missing-python-for-test"),cwd:root});
  assert.equal((await deadline(failed.completion)).errorCode, "CLIENT_REQUEST_FAILED");
  const next = createClientHost(browser).startTask({python,cwd:root,operation:"login"});
  next.cancel();
  await deadline(next.completion);
});

test("Cancel settles a hanging source, releases the lease and ignores late results", {timeout:15000}, async () => {
  const {browser} = browserFixture();
  const temporary = await realpath(await mkdtemp(join(tmpdir(),"hoyo-cancel-source-")));
  let entered, finish, signal;
  const reading = new Promise(resolve => { entered = resolve; });
  const host = createClientHost(browser, {readSource: query => {
    signal = query.signal;
    entered();
    return new Promise(resolve => { finish = resolve; }); // Deliberately ignores abort.
  }});
  const task = host.startTask({python,cwd:root,operation:"discover",
    runtimeRoot:join(temporary,"runtime"),storageRoot:join(temporary,"results")});
  const driving = (async () => {
    while (!task.status().done) await task.pump({waitMs:25});
  })();
  try {
    await deadline(reading);
    task.cancel();
    task.cancel();
    const result = await deadline(task.completion);
    await deadline(driving);
    assert.equal(result.errorCode,"LOGIN_CANCELLED");
    assert.ok(signal.aborted);
    const before = task.status();
    const next = createClientHost(browser).startTask({python,cwd:root,operation:"login"});
    next.cancel();
    await deadline(next.completion);
    finish(fixtures.genshin.binding);
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(task.status(),before);
    const {existsSync} = await import("node:fs");
    assert.equal(existsSync(join(temporary,"results")),false);
  } finally {
    task.cancel();
    await deadline(task.completion);
    await deadline(driving);
    await rm(temporary,{recursive:true,force:true});
  }
});

test("Cancel also settles a hanging browser SDK call", {timeout:15000}, async () => {
  const {browser} = browserFixture();
  let entered;
  const opening = new Promise(resolve=>{entered=resolve;});
  browser.tabs.list = () => { entered(); return new Promise(()=>{}); };
  const temporary = await realpath(await mkdtemp(join(tmpdir(),"hoyo-cancel-sdk-")));
  const host = createClientHost(browser);
  const task = host.startTask({python,cwd:root,operation:"login",runtimeRoot:join(temporary,"runtime")});
  const driving = (async()=>{while (!task.status().done) await task.pump({waitMs:25});})();
  try {
    await deadline(opening);
    host.cancel();
    assert.equal((await deadline(task.completion)).errorCode,"LOGIN_CANCELLED");
    await deadline(driving);
  } finally {
    task.cancel();
    await deadline(task.completion);
    await deadline(driving);
    await rm(temporary,{recursive:true,force:true});
  }
});

test("Closing a page while waiting is detected by the next active pump", {timeout:15000}, async () => {
  const fixture = browserFixture();
  const temporary = await realpath(await mkdtemp(join(tmpdir(),"hoyo-close-waiting-")));
  const host = createClientHost(fixture.browser);
  const task = host.startTask({python,cwd:root,operation:"login",runtimeRoot:join(temporary,"runtime")});
  try {
    await pumpUntil(task,()=>!!host.pendingQuestion());
    fixture.removePage();
    await pumpUntil(task,()=>task.status().done);
    const result = await deadline(task.completion);
    assert.equal(result.code,1);
    assert.ok(result.events.includes("ERROR_CODE=CLIENT_PAGE_UNAVAILABLE"));
    assert.equal(host.pendingQuestion(),null);
  } finally {
    task.cancel();
    await deadline(task.completion);
    await rm(temporary,{recursive:true,force:true});
  }
});

test("Confirmation is tied to one login attempt; waiting does not create a new attempt", async () => {
  const {browser} = browserFixture();
  const host = createClientHost(browser);
  const attached = await host.exchange(request(1,"session.attach",null));
  let id = 1;
  const call = (method,params)=>host.exchange(request(++id,method,attached.session,params));
  const first = await call("login.open",{url:"https://act.mihoyo.com/ys/event/calculator/index.html#/character"});
  let confirmation = call("login.confirm",{page:first.result.page,question:"Finished?"});
  for (let i=0;i<100 && !host.pendingQuestion();i++) await new Promise(r=>setTimeout(r,1));
  const attempt = host.pendingQuestion().attempt;
  await assert.rejects(host.answerLogin("completed"),/CLIENT_PROTOCOL_ERROR/);
  await host.answerLogin("waiting",attempt);
  assert.equal((await confirmation).result,"waiting");
  confirmation = call("login.confirm",{page:first.result.page,question:"Finished?"});
  for (let i=0;i<100 && !host.pendingQuestion();i++) await new Promise(r=>setTimeout(r,1));
  assert.equal(host.pendingQuestion().attempt,attempt);
  await host.answerLogin("completed",attempt);
  assert.equal((await confirmation).result,"completed");
  const second = await call("login.open",{url:"https://act.mihoyo.com/ys/event/calculator/index.html#/character"});
  confirmation = call("login.confirm",{page:second.result.page,question:"Finished?"});
  for (let i=0;i<100 && !host.pendingQuestion();i++) await new Promise(r=>setTimeout(r,1));
  assert.notEqual(host.pendingQuestion().attempt,attempt);
  await assert.rejects(host.answerLogin("completed",attempt),/CLIENT_PROTOCOL_ERROR/);
  assert.ok(host.pendingQuestion());
  await host.answerLogin("completed",host.pendingQuestion().attempt);
  assert.equal((await confirmation).result,"completed");
});

async function syntheticWorker(temporary, code) {
  await mkdir(join(temporary,"hoyo_roster"));
  await writeFile(join(temporary,"hoyo_roster","__init__.py"),"");
  await writeFile(join(temporary,"hoyo_roster","__main__.py"),code);
}

test("Oversized unfinished frames and EOF terminate without waiting for a source", {timeout:15000}, async () => {
  for (const code of ["import os\nos.write(1,b'{')\n",
    "import os,time\nos.write(1,b'x'*(16*1024*1024+1))\ntime.sleep(30)\n"]) {
    const {browser} = browserFixture();
    const temporary = await realpath(await mkdtemp(join(tmpdir(),"hoyo-bad-frame-")));
    let task;
    try {
      await syntheticWorker(temporary,code);
      task = createClientHost(browser).startTask({python,cwd:temporary});
      const result = await deadline(task.completion);
      assert.equal(result.errorCode,"CLIENT_PROTOCOL_ERROR");
      assert.equal(task.status().done,true);
    } finally {
      task?.cancel();
      if (task) await deadline(task.completion);
      await rm(temporary,{recursive:true,force:true});
    }
  }
});

test("Cancellation reaps a process which ignores graceful termination", {timeout:15000,skip:process.platform==="win32"}, async () => {
  const {browser} = browserFixture();
  const temporary = await realpath(await mkdtemp(join(tmpdir(),"hoyo-stubborn-worker-")));
  let task;
  try {
    await syntheticWorker(temporary,"import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint('{\"event\":\"report\",\"line\":\"READY\"}',flush=True)\ntime.sleep(30)\n");
    task = createClientHost(browser).startTask({python,cwd:temporary});
    for (let i=0;i<200 && !task.status().events.includes("READY");i++) await new Promise(r=>setTimeout(r,10));
    assert.ok(task.status().events.includes("READY"));
    task.cancel();
    assert.equal((await deadline(task.completion)).errorCode,"LOGIN_CANCELLED");
  } finally {
    task?.cancel();
    if (task) await deadline(task.completion);
    await rm(temporary,{recursive:true,force:true});
  }
});

test("A real worker stops both games when the browser fails during login recovery", {timeout:15000}, async () => {
  const {browser} = browserFixture();
  browser.tabs.list = async () => { throw new Error("Synthetic SDK failure"); };
  const temporary = await realpath(await mkdtemp(join(tmpdir(),"hoyo-login-failure-")));
  const reads = [];
  const host = createClientHost(browser,{readSource: async query => {
    reads.push([query.game,query.source]);
    const error = new Error("AUTHENTICATION_REQUIRED");
    error.code = "AUTHENTICATION_REQUIRED";
    throw error;
  }});
  const task = host.startTask({python,cwd:root,operation:"discover",runtimeRoot:join(temporary,"runtime")});
  try {
    await pumpUntil(task,()=>task.status().done);
    const result = await deadline(task.completion);
    assert.equal(result.code,1);
    assert.deepEqual(reads,[["genshin","binding"]]);
    assert.ok(result.events.includes("ERROR_CODE=CLIENT_REQUEST_FAILED"));
    assert.ok(!result.events.includes("GAME=zzz"));
  } finally {
    task.cancel();
    await deadline(task.completion);
    await rm(temporary,{recursive:true,force:true});
  }
});

test("A cancellation answer never waits for browser liveness", async () => {
  const {browser} = browserFixture();
  const host = createClientHost(browser);
  const attached = await host.exchange(request(1,"session.attach",null));
  const opened = await host.exchange(request(2,"login.open",attached.session,
    {url:"https://act.mihoyo.com/ys/event/calculator/index.html#/character"}));
  const confirmation = host.exchange(request(3,"login.confirm",attached.session,
    {page:opened.result.page,question:"Finished?"}));
  for (let i=0;i<100 && !host.pendingQuestion();i++) await new Promise(r=>setTimeout(r,1));
  browser.tabs.list = () => new Promise(()=>{});
  await deadline(host.answerLogin("cancel",host.pendingQuestion().attempt));
  assert.equal((await deadline(confirmation)).result,"cancel");
});
