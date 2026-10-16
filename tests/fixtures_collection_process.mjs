// Synthetic browser sources, real control subprocess/worker/session/store.
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createClientHost } from "../src/hoyo_roster/client_host.mjs";
import { recordCollection } from "../src/hoyo_roster/client_collection.mjs";
import { createWorkflowControl, createWorkflowDriver, resolveWorkflowOwner } from "../src/hoyo_roster/client_workflow.mjs";
import { record } from "./fixtures_collection_source.mjs";

const [directory, python, mode] = process.argv.slice(2);
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const owner = await resolveWorkflowOwner(python);
if (owner.pid !== process.pid) throw Error("controller launch parent mismatch");
const fresh = ["interrupt", "parallel"].includes(mode);
const control = createWorkflowControl(python, directory, owner);
const reads = [];
let initial;
for (let n = 0; n < 40; n++) {
  try {
    initial = await control(fresh ? "create" : "claim", { owner,
      ...(fresh ? { options: { runtimeRoot: join(dirname(directory), "runtime"), parallelGames: mode === "parallel" } } : {}) });
    break;
  } catch (error) {
    if (error.code !== "WORKFLOW_WORKER_STILL_RUNNING") throw error;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}
if (!initial) throw Error("synthetic old worker did not close");
const spans = [];
const driver = createWorkflowDriver({ directory, initial, control, startSession: async data => {
  const host = createClientHost({ tabs: { list: async()=>[], get:async()=>{}, new:async()=>{} } },
    { pageReader: true, readSource: async q => {
      reads.push([q.game, q.source]);
      const span = { game: q.game, start: performance.now(), end: null }; spans.push(span);
      if (mode === "parallel") await new Promise(resolve => setTimeout(resolve, 2150));
      span.end = performance.now(); return record(q.game, q.source);
    } });
  const task = host.startTask({ python, cwd: root, storageRoot: join(directory, "results"),
    runtimeRoot: data.config.runtime_root, collectionRun: directory, parallelGames: data.config.parallel_games });
  return recordCollection({ pendingQuestion: host.pendingQuestion, answerLogin: host.answerLogin,
    drainMetrics:()=>[], releasePageSettings:async()=>{} }, task,
    join(directory,"attempts",String(data.attempt).padStart(4,"0")), data.version, data.config.parallel_games,
    { onStep: row => {
      if (mode === "interrupt" && row.events.includes("PAGE_GAME_SAVED=genshin")) process.exit(0);
    } });
} });
let status;
const attachedBrowser = { synthetic: true };
do { status = await driver.advance(attachedBrowser); }
while (!status.done && status.nextAction !== "CONFIRM_LOGIN");
if (status.state !== "SUCCEEDED") throw Error("synthetic workflow failed: " + status.errorCode);
console.log(JSON.stringify({ state: status.state, reads, spans }));
