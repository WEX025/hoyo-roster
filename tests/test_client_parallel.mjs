import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, realpath, rm, readFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createClientHost } from "../src/hoyo_roster/client_host.mjs";
import { pumpWindow } from "../src/hoyo_roster/client_runner.mjs";
import { recordCollection } from "../tools/timed_collection.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const python = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const browser = () => ({ tabs: { list: async () => [], get: async () => {}, new: async () => {} } });
import { record } from "./fixtures_collection_source.mjs";

test("one real worker multiplexes games, throttles actual starts and commits both double passes", {timeout:90000}, async () => {
  const temp = await realpath(await mkdtemp(join(tmpdir(),"hoyo-parallel-")));
  const active = new Set(), starts = [], reads = [], completions = [];
  let peak = 0, insidePump = false;
  const host = createClientHost(browser(), { pageReader:true, readSource:async q=>{
    assert.ok(insidePump);
    assert.equal(active.has(q.game),false);
    active.add(q.game);peak=Math.max(peak,active.size); starts.push(performance.now());reads.push([q.game,q.source]);
    try { await sleep(q.game === "genshin" ? 2250 : 20);q.signal.throwIfAborted();return record(q.game,q.source); }
    finally {active.delete(q.game);completions.push(q.game);}
  } });
  const task=host.startTask({python,cwd:root,parallelGames:true,runtimeRoot:join(temp,"runtime"),storageRoot:join(temp,"results")});
  const logs = join(temp, "timing");
  await mkdir(logs, {mode:0o700});
  let released = false;
  const recorder = await recordCollection({drainMetrics:()=>[], releasePageSettings:async()=>{released=true;}},
    task, logs, {source_sha:"synthetic"}, true);
  try {
    while(!task.status().done){
      insidePump=true;
      try{await recorder.advance({launchMs:10000});}finally{insidePump=false;}
      assert.equal(active.size,0,"active browser work must be drained before returning");
    }
    const result=await task.completion;
    assert.equal(result.code,0,result.events.join("\n"));
    assert.equal(peak,2);
    assert.equal(completions[0],"zzz","out-of-order replies must stay correlated");
    for(let n=1;n<starts.length;n++)assert.ok(starts[n]-starts[n-1]>=1995);
    for(const game of ["genshin","zzz"]){
      assert.equal(reads.filter(([g,s])=>g===game&&s==="page:roster").length,2);
      assert.equal(reads.filter(([g,s])=>g===game&&s==="page:detail").length,2);
    }
    assert.equal(result.events.filter(x=>x==="COMMIT_STATE=COMMITTED").length,2);
    assert.ok(result.events.includes("TASK_STATUS=PASS"));
    assert.ok(result.events.some(x=>x.startsWith("CLIENT_TIMING=")));
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
    assert.ok(calls.every(q=>q.signal.aborted));
    release.forEach(fn=>fn());
    await sleep(20);
    assert.equal(calls.length,2);
    assert.equal(result.events.includes("TASK_STATUS=PASS"),false);
  }finally{if(!task.status().done){task.cancel();await task.completion;}await rm(temp,{recursive:true,force:true});}
});
