import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp,realpath,rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname,join } from "node:path";
import { fileURLToPath } from "node:url";

test("packaged entrypoint prepares without process globals or node:process",{timeout:30000},async()=>{
  const root=dirname(dirname(fileURLToPath(import.meta.url)));
  const python=join(root,".venv",process.platform==="win32"?"Scripts/python.exe":"bin/python");
  const temp=await realpath(await mkdtemp(join(tmpdir(),"hoyo-sandbox-")));
  try{
    const result=await promisify(execFile)(process.execPath,["--experimental-vm-modules",
      join(root,"tests/fixtures_workflow_sandbox.mjs"),join(temp,"run"),python],{timeout:25000});
    assert.equal(result.stdout.trim(),"SANDBOX_WORKFLOW=PASS");
  }finally{await rm(temp,{recursive:true,force:true});}
});
