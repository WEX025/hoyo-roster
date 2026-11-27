import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, realpath, rm, readFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createClientHost } from "../src/hoyo_roster/client_host.mjs";
import { sourceStep, sourceFailure } from "../src/hoyo_roster/client_network.mjs";
import { pumpWindow } from "../src/hoyo_roster/client_runner.mjs";
import { recordCollection } from "../tools/timed_collection.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const python = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const browser = () => ({ tabs: { list: async () => [], get: async () => {}, new: async () => {} } });
import { record } from "./fixtures_collection_source.mjs";

for (const parallelGames of [undefined, false]) test(`one real worker ${parallelGames === false ? "explicitly serializes" : "defaults to overlapping"} games and commits both double passes`, {timeout:90000}, async () => {
  const parallel = parallelGames !== false;
  const temp = await realpath(await mkdtemp(join(tmpdir(),"hoyo-parallel-")));
  const active = new Set(), starts = [], reads = [], completions = [], characters = [];
  let peak = 0, insidePump = false, observedIndependentCommit = false;
  const host = createClientHost(browser(), { pageReader:true, readSource:async q=>{
    assert.ok(insidePump);
    assert.equal(active.has(q.game),false);
    active.add(q.game);peak=Math.max(peak,active.size); starts.push(performance.now());reads.push([q.game,q.source]);
    try {
      if (parallel && reads.length === 1 && q.game === "genshin") {
        // Keep the identity open across timing windows until the peer has saved.
        const deadline = performance.now() + 35000;
        while (recorder.gameStates().zzz !== "COMPLETED" && performance.now() < deadline) await sleep(20);
        assert.deepEqual(recorder.gameStates(), { genshin: "CHECKING_IDENTITY", zzz: "COMPLETED" });
        observedIndependentCommit = true;
      }
      await sleep(q.game === "genshin" ? 2250 : 20);
      q.signal.throwIfAborted();
      return record(q.game, q.source);
    }
    finally {active.delete(q.game);completions.push(q.game);}
  } });
  const task=host.startTask({python,cwd:root,onCharacters: event => characters.push(event),...(parallelGames === undefined ? {} : {parallelGames}),runtimeRoot:join(temp,"runtime"),storageRoot:join(temp,"results")});
  assert.equal(task.status().parallelGames, parallel);
  const logs = join(temp, "timing");
  await mkdir(logs, {mode:0o700});
  let released = false;
  const recorder = await recordCollection({drainMetrics:()=>[], releasePageSettings:async()=>{released=true;}},
    task, logs, {source_sha:"synthetic"}, parallel);
  try {
    while(!task.status().done){
      insidePump=true;
      try{await recorder.advance({launchMs:parallel ? 40000 : 10000});}finally{insidePump=false;}
      assert.equal(active.size,0,"active browser work must be drained before returning");
    }
    const result=await task.completion;
    assert.equal(result.code,0,result.events.join("\n"));
    assert.equal(peak,parallel ? 2 : 1);
    assert.equal(observedIndependentCommit, parallel);
    assert.equal(completions[0],parallel ? "zzz" : "genshin","replies must stay correlated in either mode");
    if (parallel) for (let n=1; n<starts.length; n++) {
      const minimum=reads[n][0]===reads[n-1][0] ? 1995 : 995;
      assert.ok(starts[n]-starts[n-1]>=minimum);
    }
    for(const game of ["genshin","zzz"]){
      assert.equal(reads.filter(([g,s])=>g===game&&s==="page:roster").length,2);
      assert.equal(reads.filter(([g,s])=>g===game&&s==="page:detail").length,2);
    }
    assert.equal(result.events.filter(x=>x==="COMMIT_STATE=COMMITTED").length,2);
    assert.ok(result.events.includes("TASK_STATUS=PASS"));
    assert.ok(result.events.some(x=>x.startsWith("CLIENT_TIMING=")));
    assert.deepEqual(result.gameStates, { genshin: "COMPLETED", zzz: "COMPLETED" });
    for (const game of ["genshin", "zzz"]) {
      const batches = characters.filter(event => event.game === game);
      assert.deepEqual(batches.map(event => event.state), ["PREVIEW", "MATCHED", "COMMITTED"]);
      assert.deepEqual(batches[0].records, batches[1].records);
      assert.equal(batches[0].records[0].name, "Alpha");
      assert.equal(reads.filter(([g,s])=>g===game&&s==="page:identity").length,3);
    }
    for (const file of ["events.jsonl", "result.json", "steps.md"]) {
      assert.ok(!(await readFile(join(logs,file),"utf8")).includes('Alpha'));
    }
    assert.equal(released,true);
    assert.equal(JSON.parse(await readFile(join(logs,"result.json"),"utf8")).code,0);
    await assert.rejects(readFile(join(temp,"results","snapshot.json")),{code:"ENOENT"});
  }finally{if(!task.status().done){task.cancel();await task.completion;}await rm(temp,{recursive:true,force:true});}
});

test("cancelling an overlapped worker aborts both sources and discards late replies", {timeout:15000}, async () => {
  const temp = await realpath(await mkdtemp(join(tmpdir(),"hoyo-parallel-cancel-")));
  const calls = [], release = [];
  const host = createClientHost(browser(), {pageReader:true,readSource:q=>new Promise(resolve=>{
    calls.push(q);release.push(()=>resolve(record(q.game,q.source)));
  })});
  const task=host.startTask({python,cwd:root,parallelGames:true,runtimeRoot:join(temp,"runtime"),storageRoot:join(temp,"results")});
  try{
    const pumping=pumpWindow(task);
    const deadline=Date.now()+7000;
    while(calls.length<2&&Date.now()<deadline)await sleep(10);
    assert.equal(calls.length,2);
    task.cancel();
    await pumping;
    const result=await task.completion;
    assert.equal(result.code,1);
    assert.deepEqual(result.gameStates, { genshin: "CANCELLED", zzz: "CANCELLED" });
    assert.ok(calls.every(q=>q.signal.aborted));
    release.forEach(fn=>fn());
    await sleep(20);
    assert.equal(calls.length,2);
    assert.equal(result.events.includes("TASK_STATUS=PASS"),false);
  }finally{if(!task.status().done){task.cancel();await task.completion;}await rm(temp,{recursive:true,force:true});}
});

test("startup authentication drains the stale page action before login and resumes both games", {timeout:90000}, async () => {
  const temp = await realpath(await mkdtemp(join(tmpdir(), "hoyo-parallel-login-")));
  let peerDrained = false, confirmations = 0;
  const page = { id: "login", markHandoff: async () => assert.ok(peerDrained) };
  const tabs = { list: async () => [{ id: page.id, url: "https://act.mihoyo.com/ys/event/calculator/index.html" }],
    get: async () => page, new: async () => { throw Error("unexpected new login tab"); } };
  let firstGame, probes = 0;
  const business = [];
  const host = createClientHost({ tabs }, { pageReader: true, readSource: query => sourceStep(query, async q => {
    if (!q.generation) {
      assert.equal(q.source, "page:identity");
      if (firstGame) {
        if (q.recheck) probes++;
        throw sourceFailure("AUTHENTICATION_REQUIRED");
      }
      firstGame = q.game;
      try {
        await new Promise(resolve => q.signal.addEventListener("abort", resolve, { once: true }));
        await sleep(20); // An SDK action already in progress must settle before handing off.
        q.signal.throwIfAborted();
      } finally { peerDrained = true; }
    }
    assert.equal(q.generation, 1);
    if (q.source !== "page:identity") {
      const span = { game: q.game, source: q.source, start: performance.now() };
      business.push(span);
      await sleep(2300);
      span.end = performance.now();
    }
    const result = record(q.game, q.source);
    result.evidence.generation = q.generation;
    return result;
  }) });
  const task = host.startTask({ python, cwd: root, runtimeRoot: join(temp, "runtime"), storageRoot: join(temp, "results") });
  try {
    while (!task.status().done) {
      const status = await pumpWindow(task);
      if (status.question) {
        assert.equal(++confirmations, 1);
        await host.answerLogin("completed", status.question.attempt);
      }
    }
    const result = await task.completion;
    assert.equal(result.code, 0, result.events.join("\n"));
    assert.equal(confirmations, 1);
    assert.equal(probes, 1);
    assert.ok(result.events.includes("COLLECTION_MODE=PARALLEL"));
    assert.ok(!result.events.includes("COLLECTION_MODE=SERIAL_RECOVERY"));
    for (const game of ["genshin", "zzz"]) {
      for (const source of ["page:roster", "page:detail"]) {
        assert.equal(business.filter(s => s.game === game && s.source === source).length, 2);
      }
    }
    assert.ok(business.some((a, i) => business.slice(i + 1).some(b => a.game !== b.game && b.start < a.end)));
  } finally {
    if (!task.status().done) { task.cancel(); await task.completion; }
    await rm(temp, { recursive: true, force: true });
  }
});

test("a transient identity failure refreshes only its own game while its peer completes", {timeout:20000}, async () => {
  const temp = await realpath(await mkdtemp(join(tmpdir(), "hoyo-local-login-")));
  let peer, peerFinished = false, probes = 0;
  const host = createClientHost(browser(), { pageReader: true, readSource: async q => {
    assert.equal(q.generation, 0);
    assert.equal(q.source, "page:identity");
    if (!peer) {
      peer = q.game;
      await sleep(5500);
      q.signal.throwIfAborted();
      peerFinished = true;
    } else {
      assert.notEqual(q.game, peer);
      if (!q.recheck) throw sourceFailure("AUTHENTICATION_REQUIRED");
      probes++;
    }
    return record(q.game, q.source);
  }});
  const task = host.startTask({ python, cwd: root, operation: "discover", runtimeRoot: join(temp, "runtime") });
  try {
    while (!task.status().done) assert.equal((await pumpWindow(task)).question, null);
    const result = await task.completion;
    assert.equal(result.code, 0, result.events.join("\n"));
    assert.ok(peerFinished);
    assert.equal(probes, 1);
    assert.ok(!result.events.some(event => event === "PARALLEL_RECOVERY=QUIESCED"));
  } finally {
    if (!task.status().done) { task.cancel(); await task.completion; }
    await rm(temp, { recursive: true, force: true });
  }
});

test("staggering shortens cross-game launches and preserves same-game spacing", {timeout:50000}, async () => {
  const temp = await realpath(await mkdtemp(join(tmpdir(), "hoyo-stagger-")));
  const host = createClientHost(browser(), {pageReader:true, readSource:async q=>record(q.game,q.source)});
  const task = host.startTask({python,cwd:root,
    runtimeRoot:join(temp,"runtime"),storageRoot:join(temp,"results")});
  try {
    while (!task.status().done) await pumpWindow(task);
    const result = await task.completion;
    assert.equal(result.code,0);
    assert.equal(result.crossGameIntervalMs,1000);
    const calls=result.events.filter(e=>e.startsWith("CLIENT_TIMING=")).map(e=>JSON.parse(e.slice(14)))
      .filter(e=>e.method==="source.read").sort((a,b)=>a.started-b.started);
    assert.ok(calls.length >= 12 && calls.every(e=>e.pageRead));
    assert.ok(calls.some((b,i)=> i && b.game!==calls[i-1].game && b.started-calls[i-1].started<1900));
    const byGame=new Map();
    for(const [i,c] of calls.entries()){
      if(i) assert.ok(c.started-calls[i-1].started>=999);
      if(byGame.has(c.game)) assert.ok(c.started-byGame.get(c.game)>=1999);
      byGame.set(c.game,c.started);
    }
  } finally {
    if(!task.status().done){task.cancel();await task.completion;}
    await rm(temp,{recursive:true,force:true});
  }
});
