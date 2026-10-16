// Match the desktop's missing process global and forbidden node:process import.
import { SourceTextModule, SyntheticModule, createContext } from "node:vm";
import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";

const [directory, python] = process.argv.slice(2);
const url = new URL("../src/hoyo_roster/client_workflow.mjs", import.meta.url);
const context = createContext({ performance });
const module = new SourceTextModule(await readFile(url,"utf8"), {
  context, initializeImportMeta: meta => { meta.url=url.href; },
});
await module.link(async specifier => {
  if(specifier === "node:process")throw Error("sandbox forbids node:process");
  const exports=await import(specifier), names=Object.keys(exports);
  return new SyntheticModule(names,function(){for(const name of names)this.setExport(name,exports[name]);},{context});
});
await module.evaluate();
const workflow = await module.namespace.openCollectionWorkflow({directory,python,runtimeRoot:directory+"-runtime"});
const status = await workflow.advance();
if(status.state!=="WAITING_BROWSER")throw Error("sandbox workflow failed: "+status.errorCode);
await workflow.cancel();
console.log("SANDBOX_WORKFLOW=PASS");
