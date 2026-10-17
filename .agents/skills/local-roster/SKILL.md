---
name: local-roster
description: Collect owned Genshin/ZZZ data through the current client's built-in browser, with a durable timed workflow, manual login, double-pass verification and private partial snapshots. HSR is paused.
---

Read [AGENTS](../../../AGENTS.md). Use the current client's documented browser tools to obtain the actual built-in browser object; never launch a separate browser or export authentication material. Codex uses its in-app browser. For field limits read [page observations](../../../docs/page-observations.md); for the entrypoint and recovery contract read [durable workflow](../../../docs/timed-collection.md).

## Single entrypoint

Run `hoyo-roster-workflow --entrypoint` (or `python -m hoyo_roster.collection_control --entrypoint`) to obtain the selected package entrypoint and interpreter. Import its `client_workflow.mjs` and call `openCollectionWorkflow({ directory, ...options })` once. Choose a new private run directory outside the checkout. Python 3.12 is discovered from the package environment or source `.venv`; supply `python` only when needed. Reuse the same `runtimeRoot` across runs if overriding the platform default. `parallelGames:true` enables the user's requested parallel mode; the default remains serial. Pass `update:true` only when the user's request includes updating the checkout. The workflow owns source checks, authorized fast-forward, necessary reinstall/recheck, version-pinned runtime, collection, cleanup, verification, reporting and stage timings. Do not reimplement these steps or call the legacy experimental scripts.

In each active foreground tool call, explicitly await `workflow.advance(browser)`. Retain the SAME object for subsequent calls. Each call returns `state`, `nextAction`, `done`, fixed progress, and an attempt-scoped login question when needed. Use a 270-second tool timeout on the tested desktop; collection has a 180-second launch budget plus bounded source drain. Preparation returns separately to leave the collection budget intact. No detached loops or background browser work.

On Windows, use a Python 3.12 environment's `Scripts/python.exe` returned by `--entrypoint`; the workflow resolves the verified venv redirector in its platform layer. Use `parallelGames:true` when parallel collection is requested, within this one workflow and one shared `runtimeRoot`. Do not launch one workflow per game or replace the interpreter with a base Python to evade ownership checks. Keep the source checkout clean and installed files matched before live acceptance. The anchored checkout is trusted only for each Git invocation; do not add global `safe.directory` entries. See [Windows workflow validation](../../../docs/timed-collection.md#windows).

Windows worker scheduling uses the package's IOCP pipe loop through `platform_io.run_async`; installation uses a private run-scoped temporary directory. A startup with no source progress needs diagnosis and normal cancellation, not extra workflows, global network changes, or blindly killing Python processes. For synthetic native tests select a fresh workspace `--basetemp`; inherited desktop sandbox TEMP paths may be inaccessible.

| nextAction | Required action |
| --- | --- |
| CONTINUE | Immediately call the same `advance` in the next active call; do not run separate analysis commands |
| ATTACH_BROWSER | Obtain the current supported browser object and pass it to `advance` |
| CONFIRM_LOGIN | Ask the login question below, preserving the exact opaque `question.attempt` |
| DONE | Report the automatically generated report and actual partial counts; inspect no unrelated result directories |
| STOP | Report the fixed error/cancellation; do not claim success or blindly retry |

## Login wait

Proactively ask once for each new `question.attempt`: 已完成登录 / 尚未完成 / 取消. The user handles phone, OTP, QR, agreements, image verification and submission in the official page. Do not request or enter credentials, send SMS, check agreements or read login input values. After explicit completion for THIS attempt, call `await workflow.answerLogin("completed", question.attempt)` and continue. If already confirmed for this attempt, use that confirmation without another prompt. A later attempt needs fresh confirmation. Elapsed time is never confirmation.

For 尚未完成 call `answerLogin("waiting", question.attempt)` and wait for user input; this retains the suspended question and does not produce repeated prompts. For cancellation call `await workflow.cancel()`. The workflow retains the shared browser; login confirmation alone does not finish collection.

## Recovery and delivery

After a client process interruption, obtain a fresh supported browser object, import the entrypoint again, and call `openCollectionWorkflow({directory, python?})` with the SAME run directory and no changed options. It rejects live prior controllers/workers, pins the same code version, reads current official identity and verifies committed snapshot hashes. Completed games only recheck identity; uncommitted games repeat both passes. Browser objects, credentials and login approvals are never serialized. Same-client bindings lost while their original process remains alive may return WORKFLOW_BUSY; do not kill a PID blindly or start a replacement run to evade the lease.

Automatic report stages resume offline. Explicit `workflow.retry()` is limited to recoverable failures or a user-resumed cancellation; identity, risk-control and checkpoint failures cannot be blindly retried. Tool failure while the object is still available requires `await workflow.cancel()` in an active call. Use `workflow.status()` for fixed progress. A source or pipeline failure is never successful collection.

Results remain OFFICIAL_PAGE / PARTIAL, including actual unsupported rows. Genshin excludes artifacts/full stat panels; HSR is paused. Full API collection, other clients/platforms and restart retention need their own acceptance evidence. For standalone login or ordinary low-level operations read [client bridge](../../../docs/client-bridge.md); for offline maintenance read [local use](../../../docs/local-use.md). Do not treat synthetic tests as live acceptance or upload private logs/results.
