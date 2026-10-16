---
name: local-roster
description: Collect owned Genshin/ZZZ page data and save private partial snapshots through the AI client's built-in browser, sharing manual login across games. HSR is paused.
---

Read [AGENTS.md](../../../AGENTS.md), [workflow](../../../docs/workflow.md) and [client bridge](../../../docs/client-bridge.md).

Use the current AI client's built-in browser on every OS; Codex uses its in-app browser. Bootstrap and select it through the host's documented browser tools, then retain the actual browser object. Never pass only its ID, launch a separate browser, export cookies or read authentication databases.

## Start the connected task

The installed Python package must match the selected source. Do not substitute a stale installed package. Missing client connection gives CLIENT_BROWSER_REQUIRED. Each game checks its own required sources before identity reads or business writes; a failed game's capability check does not skip the next game.

For sync/discover, import src/hoyo_roster/client_runtime.mjs and call createRosterHost(browser). It assembles game-owned DOM readers and the existing API readers; do not use an unconfigured low-level host or ask the executing agent to implement readSource. The default page path does not require CDP. It reads official UI identity, owned rows and current details twice, validates and atomically saves a partial page snapshot. Read [page observations](../../../docs/page-observations.md) for format and field boundaries. If the selected client lacks DOM access, games separately check the API path's advertised capabilities. Never invent API responses, HTTP status, numeric IDs or completeness evidence from DOM observations.

For standalone login, import src/hoyo_roster/client_host.mjs and createClientHost(browser); login has no data-reader dependency. Use host.startTask with the selected Python 3.12 environment, repository cwd and requested game/operation; default sync handles Genshin then ZZZ. storageRoot and runtimeRoot are optional documented overrides. The host starts a private worker pipe; do not run --client-stdio in a visible terminal.

## Login module

Explicitly await task.pump({maxRequests: 1}) inside each active client tool call. For bounded consecutive pumping, import client_runner.mjs and await pumpWindow(task) in that active call; its ten-second launch window plus a forty-five-second in-flight source needs at most fifty-five seconds. It returns immediately on a login question or task completion and never performs background browser work. Pipe callbacks only queue requests; they must never call browser tools in the background. Each bundled source step has a 45-second total deadline, including browser preparation; cancellation prevents later browser actions. Pump returns when human confirmation is needed. For each new host.pendingQuestion().attempt, proactively ask whether the person finished login in the official page, using the available user-input tool. Retain that attempt value with the question; suppress repeated prompts for the same waiting attempt. Choices: 已完成登录 / 尚未完成 / 取消. The user handles phone, OTP, QR confirmation, agreements, image verification and submission in the page; do not solicit or enter credentials, send SMS or check agreements.

Keep host/task bindings and the same browser/page alive while waiting. Call host.answerLogin("completed", question.attempt) only after explicit confirmation for that attempt. If the person already said “已登录” for this attempt, use that answer without asking again. A new login attempt after explicit expiry requires a fresh confirmation; never reuse an earlier attempt's answer. Use host.answerLogin("waiting", question.attempt) for an explicit not-yet response and wait for the person; do not repeatedly re-prompt. Use host.answerLogin("cancel", question.attempt) or task.cancel() on cancellation. Elapsed time is never a confirmation.

The login module returns at user confirmation, with the same session/page; no identity probes, roster reads or business writes occur inside it. It does not close the shared browser.

## Continue the game task

Keep pumping until done; each game owns identity checks, both passes, normalization, validation and saving. The page path reads the official role selector, never substitutes user confirmation for identity, and currently requires a unique bound role. API-mode ZZZ alone owns service initialization/refresh. Both paths reuse the same session and restart only the current uncommitted game once on auth recovery; completed games stay saved. Login confirmation is not the end of a collection task.

Inspect task.status() for fixed progress, questions and errors. Use game=all for sequential Genshin then ZZZ; drain host.drainMetrics() into local private timing records, separately from PAGE_TIMING worker measurements, without summing nested or overlapping spans. task.completion settles after process closure; check its exit code and protocol result. Do not await completion alone while requests need pumping. At every task end, await completion, then await host.releasePageSettings() in an active tool call; cancel first on tool failure or user cancellation. Normal pump completion also restores temporary zero-viewport repairs. Retain the shared browser and pages. Source callbacks stop on AbortSignal; source failure is not successful collection.

Report page saves as OFFICIAL_PAGE / PARTIAL, with actual counts and paths. They do not meet API complete coverage and never overwrite API generations. HSR is paused; Genshin excludes artifacts/full stat panels. For offline API-generation maintenance read [local use](../../../docs/local-use.md). Do not explore old private directories or generalize session retention to client restarts or untested platforms.
