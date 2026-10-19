import test from "node:test";
import assert from "node:assert/strict";
import * as vm from "node:vm";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import { promisify } from "node:util";
import { performance } from "node:perf_hooks";

// The public entrypoint must delegate cold install to the common policy. No
// browser or actual installation is used; stdin/JSON/error routing are real.
if (!vm.SourceTextModule) {
  test("cold bootstrap policy on both platform contracts", async () => {
    const environment = { ...process.env };
    delete environment.NODE_TEST_CONTEXT;
    const result = await promisify(execFile)(process.execPath,
      ["--experimental-vm-modules", "--test-reporter=tap", fileURLToPath(import.meta.url)], { timeout: 10000, windowsHide: true, env: environment });
    assert.match(result.stdout, /# pass 2/);
    assert.match(result.stdout, /# fail 0/);
  });
} else for (const platform of ["win32", "darwin"]) {
  test(`cold ${platform} bootstrap delegates policy without a second installer`, async () => {
    const url = new URL("../src/hoyo_roster/client_workflow.mjs", import.meta.url);
    const context = vm.createContext({ performance });
    const calls = [];
    const execute = () => {};
    execute[promisify.custom] = (file, args, options) => {
      calls.push({ file, args, options });
      if (args.some(a => a.includes("sys.version_info"))) return Promise.resolve({ stdout: "1" });
      if (args.includes("import portalocker,platformdirs,jsonschema")) return Promise.reject(Error("synthetic missing dependency"));
      assert.ok(args.some(a => a.includes("hoyo_roster.bootstrap")));
      assert.equal(file, "synthetic-python");
      const pending = new Promise(resolve => {
        execute.answer = request => {
          assert.ok(request.directory.endsWith("synthetic-run"));
          assert.ok(request.repository.endsWith("hoyo-roster"));
          resolve({ stdout: JSON.stringify({ ok: false, errorCode: "WORKFLOW_PLATFORM_DEPENDENCIES_REQUIRED" }) });
        };
      });
      pending.child = { stdin: { on() {}, end(value) { execute.answer(JSON.parse(value)); } } };
      return pending;
    };
    const module = new vm.SourceTextModule(await readFile(url, "utf8"), {
      context, initializeImportMeta: meta => { meta.url = url.href; },
    });
    await module.link(async name => {
      const actual = await import(name);
      const overrides = name === "node:child_process" ? { execFile: execute }
        : name === "node:fs/promises" ? { access: async () => { throw Error("synthetic new run"); } }
        : name === "node:os" ? { platform: () => platform } : {};
      return new vm.SyntheticModule(Object.keys(actual), function () {
        for (const key of Object.keys(actual)) this.setExport(key, overrides[key] ?? actual[key]);
      }, { context });
    });
    await module.evaluate();
    await assert.rejects(module.namespace.openCollectionWorkflow({ directory: "synthetic-run", python: "synthetic-python" }),
      { code: "WORKFLOW_PLATFORM_DEPENDENCIES_REQUIRED" });
    assert.equal(calls.length, 3);
    assert.equal(calls.some(c => c.file === "git" || c.args.includes("pip")), false);
  });
}
