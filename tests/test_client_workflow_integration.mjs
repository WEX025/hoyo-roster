import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, realpath, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execute = promisify(execFile);
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const python = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");

test("a fresh process rechecks identity, reuses a committed game and automatically reports", {timeout:120000}, async () => {
  const temp = await realpath(await mkdtemp(join(tmpdir(), "hoyo-workflow-restart-")));
  const directory = join(temp, "run");
  const fixture = join(root, "tests/fixtures_collection_process.mjs");
  try {
    await execute(process.execPath, [fixture, directory, python, "interrupt"], { timeout:60000 });
    const partial = JSON.parse(await readFile(join(directory,"workflow.json"),"utf8"));
    assert.deepEqual(Object.keys(partial.completed),["genshin"]);
    const result = await execute(process.execPath,[fixture,directory,python,"resume"],{timeout:60000});
    const output = JSON.parse(result.stdout);
    assert.equal(output.state,"SUCCEEDED");
    assert.equal(output.reads.filter(([g,s])=>g==="genshin"&&s==="page:identity").length,1);
    assert.equal(output.reads.filter(([g,s])=>g==="genshin"&&s==="page:roster").length,0);
    assert.equal(output.reads.filter(([g,s])=>g==="zzz"&&s==="page:roster").length,2);
    const final = JSON.parse(await readFile(join(directory,"workflow.json"),"utf8"));
    assert.equal(final.attempts.length,2);
    assert.equal(final.attempts[0].outcome,"INTERRUPTED");
    assert.equal(final.completed.genshin.sha256,partial.completed.genshin.sha256);
    const report = JSON.parse(await readFile(join(directory,"report/summary.json"),"utf8"));
    assert.equal(report.games.length,2);
    assert.equal(report.metrics_complete,false);
    assert.equal(JSON.stringify(report).includes("123456789"),false);
  } finally { await rm(temp,{recursive:true,force:true}); }
});
